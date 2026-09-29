"""Shikimori REST: Russian titles and synopses, fetched by MAL id.

Shikimori ids equal MAL ids for almost every entry; a few are letter-prefixed
(for example `z36098`), so ids are handled as text.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field

from pipeline.http import ItemFailed, SourceClient

SOURCE = "shikimori"


class Anime(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    id: str
    name: str | None = None
    russian: str | None = None
    license_name_ru: str | None = None
    english: list[str | None] = Field(default_factory=list)
    japanese: list[str | None] = Field(default_factory=list)
    synonyms: list[str | None] = Field(default_factory=list)
    description: str | None = None
    myanimelist_id: int | None = None


def client() -> SourceClient:
    return SourceClient(SOURCE, settings.SHIKIMORI_URL, headers={"Accept": "application/json"})


def fetch_by_id(c: SourceClient, shikimori_id: int | str) -> dict[str, Any] | None:
    """Returns the raw anime payload, or None when Shikimori has no such entry.
    Shikimori ids equal MAL ids for almost every entry."""
    try:
        response = c.request("GET", f"/animes/{shikimori_id}")
    except ItemFailed as exc:
        if exc.status == 404:
            return None
        raise
    payload: dict[str, Any] = response.json()
    return payload


fetch_by_mal_id = fetch_by_id


def search(
    c: SourceClient, text: str, *, adult: bool = False, limit: int = 10
) -> list[dict[str, Any]]:
    """Title search; returns short entries (id, name, russian, kind, episodes, aired_on)."""
    params: dict[str, Any] = {"search": text, "limit": limit}
    if adult:
        params["censored"] = "false"
    response = c.request("GET", "/animes", params=params)
    results: list[dict[str, Any]] = response.json()
    return results
