"""Annict GraphQL: Japanese titles, fetched in batches by Annict id.

Requires a personal access token (ANNICT_TOKEN); without one the source is skipped.
"""

from __future__ import annotations

from django.conf import settings
from pydantic import BaseModel, ConfigDict

from pipeline.http import ItemFailed, SourceClient

SOURCE = "annict"
BATCH_SIZE = 50

WORKS_QUERY = """
query ($ids: [Int!], $first: Int) {
  searchWorks(annictIds: $ids, first: $first) {
    nodes { annictId title titleEn titleKana titleRo malAnimeId }
  }
}
"""


class Work(BaseModel):
    model_config = ConfigDict(extra="ignore")

    annictId: int
    title: str | None = None
    titleEn: str | None = None
    titleKana: str | None = None
    titleRo: str | None = None
    malAnimeId: str | None = None


def enabled() -> bool:
    return bool(settings.ANNICT_TOKEN)


def client() -> SourceClient:
    return SourceClient(
        SOURCE,
        settings.ANNICT_URL,
        headers={"Authorization": f"Bearer {settings.ANNICT_TOKEN}"},
    )


def fetch_works(c: SourceClient, annict_ids: list[int]) -> list[dict]:
    response = c.request(
        "POST",
        "",
        json={"query": WORKS_QUERY, "variables": {"ids": annict_ids, "first": len(annict_ids)}},
    )
    body = response.json()
    if body.get("errors"):
        raise ItemFailed(SOURCE, 400, "; ".join(str(e.get("message")) for e in body["errors"]))
    nodes: list[dict] = body["data"]["searchWorks"]["nodes"]
    return nodes
