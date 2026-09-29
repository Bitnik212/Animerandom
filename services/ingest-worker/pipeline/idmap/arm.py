"""kawaiioverflow/arm: MAL / AniList / Annict id cross-reference, refreshed weekly."""

from __future__ import annotations

import httpx
from django.conf import settings
from django.db import transaction

from pipeline.models import IdSourceEntry

SOURCE = "arm"


def download() -> list[dict]:
    response = httpx.get(
        settings.ARM_URL, headers={"User-Agent": settings.HTTP_USER_AGENT}, timeout=60
    )
    response.raise_for_status()
    rows: list[dict] = response.json()
    return rows


def load(rows: list[dict]) -> int:
    entries = [
        IdSourceEntry(
            source=SOURCE,
            anilist_id=row.get("anilist_id"),
            mal_id=row.get("mal_id"),
            annict_id=row.get("annict_id"),
        )
        for row in rows
        if row.get("annict_id") and (row.get("anilist_id") or row.get("mal_id"))
    ]
    with transaction.atomic():
        IdSourceEntry.objects.filter(source=SOURCE).delete()
        IdSourceEntry.objects.bulk_create(entries, batch_size=2000)
    return len(entries)


def refresh() -> int:
    return load(download())
