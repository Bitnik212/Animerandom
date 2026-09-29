"""raw_source_record: every response is stored here before anything else happens."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from django.db import connections
from django.utils import timezone

from catalog.models import RawSourceRecord


def payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def store_raw(source: str, records: Iterable[tuple[str, Any]]) -> set[str]:
    """Upsert payloads for one source. Returns the source ids whose payload changed
    (new records included). Unchanged records only get a fresh `fetched_at`."""
    rows = [(str(source_id), payload, payload_hash(payload)) for source_id, payload in records]
    if not rows:
        return set()
    ids = [r[0] for r in rows]
    existing = dict(
        RawSourceRecord.objects.filter(source=source, source_id__in=ids).values_list(
            "source_id", "payload_hash"
        )
    )
    now = timezone.now()
    with connections["catalog"].cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO raw_source_record (source, source_id, payload, payload_hash, fetched_at)
            VALUES (%s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (source, source_id) DO UPDATE
              SET payload = EXCLUDED.payload,
                  payload_hash = EXCLUDED.payload_hash,
                  fetched_at = EXCLUDED.fetched_at
            """,
            [(source, sid, json.dumps(p, ensure_ascii=False), h, now) for sid, p, h in rows],
        )
    return {sid for sid, _, h in rows if existing.get(sid) != h}


def load_raw(source: str, source_ids: Iterable[str | int]) -> dict[str, RawSourceRecord]:
    ids = [str(i) for i in source_ids]
    return {
        r.source_id: r for r in RawSourceRecord.objects.filter(source=source, source_id__in=ids)
    }
