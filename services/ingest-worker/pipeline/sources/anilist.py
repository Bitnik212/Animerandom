"""AniList GraphQL: the backbone of the catalog."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field

from pipeline.http import ItemFailed, SourceClient, TransientError

SOURCE = "anilist"

MEDIA_FIELDS = """
  id
  idMal
  title { romaji english native }
  synonyms
  description(asHtml: false)
  format
  status
  episodes
  duration
  season
  seasonYear
  averageScore
  meanScore
  popularity
  isAdult
  updatedAt
  coverImage { extraLarge large }
  genres
  tags { name rank category isGeneralSpoiler isMediaSpoiler }
  studios { edges { isMain node { id name isAnimationStudio } } }
  relations { edges { relationType node { id type } } }
"""

PAGE_BY_ID_QUERY = (
    """
query ($perPage: Int, $idGreater: Int, $idLesser: Int, $status: MediaStatus) {
  Page(page: 1, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: ID, id_greater: $idGreater, id_lesser: $idLesser, status: $status) {"""
    + MEDIA_FIELDS
    + """}
  }
}"""
)

PAGE_BY_POPULARITY_QUERY = (
    """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: POPULARITY_DESC) {"""
    + MEDIA_FIELDS
    + """}
  }
}"""
)

PAGE_BY_UPDATED_QUERY = (
    """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: UPDATED_AT_DESC) {"""
    + MEDIA_FIELDS
    + """}
  }
}"""
)

BY_IDS_QUERY = (
    """
query ($ids: [Int], $perPage: Int) {
  Page(page: 1, perPage: $perPage) {
    media(type: ANIME, id_in: $ids) {"""
    + MEDIA_FIELDS
    + """}
  }
}"""
)


# --- Response models (used by merge; raw payloads are stored unvalidated) -------


class Title(BaseModel):
    romaji: str | None = None
    english: str | None = None
    native: str | None = None


class CoverImage(BaseModel):
    extraLarge: str | None = None
    large: str | None = None


class MediaTag(BaseModel):
    name: str
    rank: int | None = None
    category: str | None = None
    isGeneralSpoiler: bool = False
    isMediaSpoiler: bool = False


class StudioNode(BaseModel):
    id: int
    name: str
    isAnimationStudio: bool = True


class StudioEdge(BaseModel):
    isMain: bool = False
    node: StudioNode


class Studios(BaseModel):
    edges: list[StudioEdge] = Field(default_factory=list)


class RelationNode(BaseModel):
    id: int
    type: str | None = None


class RelationEdge(BaseModel):
    relationType: str | None = None
    node: RelationNode


class Relations(BaseModel):
    edges: list[RelationEdge] = Field(default_factory=list)


class Media(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    idMal: int | None = None
    title: Title = Field(default_factory=Title)
    synonyms: list[str] = Field(default_factory=list)
    description: str | None = None
    format: str | None = None
    status: str | None = None
    episodes: int | None = None
    duration: int | None = None
    season: str | None = None
    seasonYear: int | None = None
    averageScore: int | None = None
    meanScore: int | None = None
    popularity: int | None = None
    isAdult: bool = False
    updatedAt: int | None = None
    coverImage: CoverImage = Field(default_factory=CoverImage)
    genres: list[str] = Field(default_factory=list)
    tags: list[MediaTag] = Field(default_factory=list)
    studios: Studios = Field(default_factory=Studios)
    relations: Relations = Field(default_factory=Relations)


class PageInfo(BaseModel):
    hasNextPage: bool = False


class _MediaRef(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int


class Page(BaseModel):
    pageInfo: PageInfo = Field(default_factory=PageInfo)
    media: list[_MediaRef] = Field(default_factory=list)


# --- Client -------------------------------------------------------------------------


def client() -> SourceClient:
    return SourceClient(SOURCE, settings.ANILIST_URL, headers={"Accept": "application/json"})


def graphql(c: SourceClient, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = c.request("POST", "", json={"query": query, "variables": variables})
    body = response.json()
    errors = body.get("errors")
    if errors:
        status = int(errors[0].get("status") or 400)
        message = "; ".join(str(e.get("message")) for e in errors)
        if status >= 500:
            raise TransientError(f"{SOURCE}: {message}")
        raise ItemFailed(SOURCE, status, message)
    data: dict[str, Any] = body["data"]
    return data


def fetch_page(c: SourceClient, query: str, variables: dict[str, Any]) -> tuple[list[dict], bool]:
    """One Page query. Returns raw media dicts (validated for shape only) and hasNextPage."""
    data = graphql(c, query, {"perPage": settings.ANILIST_PER_PAGE, **variables})
    page = Page.model_validate(data["Page"])
    raw = data["Page"]["media"]
    return raw, page.pageInfo.hasNextPage


@dataclass(frozen=True)
class IdRange:
    """AniList ids in (start, end]. end=None means open-ended."""

    start: int
    end: int | None


def id_ranges(width: int, top: int = 300_000) -> list[IdRange]:
    """Static partition of the id space for a full run. The last range is open-ended,
    so new ids beyond `top` are still covered; empty ranges cost one request."""
    bounds = list(range(0, top, width))
    return [IdRange(s, s + width) for s in bounds[:-1]] + [IdRange(bounds[-1], None)]


def iter_id_range(
    c: SourceClient, rng: IdRange, after_id: int | None = None, status: str | None = None
) -> Iterator[list[dict]]:
    """Keyset pagination over (start, end] by id, resuming after `after_id`."""
    cursor = max(rng.start, after_id or 0)
    while True:
        variables: dict[str, Any] = {"idGreater": cursor}
        if rng.end is not None:
            variables["idLesser"] = rng.end + 1
        if status:
            variables["status"] = status
        media, has_next = fetch_page(c, PAGE_BY_ID_QUERY, variables)
        if not media:
            return
        yield media
        cursor = max(m["id"] for m in media)
        if not has_next:
            return


def fetch_by_ids(c: SourceClient, ids: list[int]) -> list[dict]:
    data = graphql(c, BY_IDS_QUERY, {"ids": ids, "perPage": max(len(ids), 1)})
    media: list[dict] = data["Page"]["media"]
    return media
