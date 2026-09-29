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


def fetch_by_mal_id(c: SourceClient, mal_id: int) -> dict[str, Any] | None:
    """Returns the raw anime payload, or None when Shikimori has no such entry."""
    try:
        response = c.request("GET", f"/animes/{mal_id}")
    except ItemFailed as exc:
        if exc.status == 404:
            return None
        raise
    payload: dict[str, Any] = response.json()
    return payload
