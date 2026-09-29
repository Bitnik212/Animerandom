"""Frozen manami-project snapshot: fills AniList → MAL gaps when AniList has no idMal."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from django.conf import settings
from django.db import transaction

from pipeline.models import IdSourceEntry

SOURCE = "manami"


def load(path: Path | None = None) -> int:
    with gzip.open(path or settings.MANAMI_SNAPSHOT, "rt") as f:
        snapshot = json.load(f)
    entries = [
        IdSourceEntry(source=SOURCE, anilist_id=anilist_id, mal_id=mal_id)
        for anilist_id, mal_id in snapshot["pairs"]
    ]
    with transaction.atomic():
        IdSourceEntry.objects.filter(source=SOURCE).delete()
        IdSourceEntry.objects.bulk_create(entries, batch_size=2000)
    return len(entries)
