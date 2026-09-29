"""Build the Celery canvas for a run.

    start_run
     → chord(fetch AniList: id ranges | popularity pages | updated + airing | ids)  source.anilist
     → map_ids
     → chord(Shikimori shards, Annict shards)            source.shikimori / source.annict
     → chord(merge_write shards, batches of 200)                      pipeline
     → link_relations → mark_removed (full) → index → pools           pipeline / index
     → finish_run

The shape is fixed when the run is created: AniList full runs use a static
partition of the id space, and later stages are sharded by anilist_id, so no
task has to know how much work the previous stage produced.
"""

from __future__ import annotations

import math
from typing import Any

from celery import Signature, chain, chord
from django.conf import settings

from pipeline import tasks
from pipeline.sources import anilist
from runs import services as runs
from runs.models import IngestRun, RunMode

SECONDARY_SHARDS = 4
MERGE_SHARDS = 4
ALL_SOURCES = ("anilist", "shikimori", "annict")
FETCH_MODES = (RunMode.FULL, RunMode.INCREMENTAL, RunMode.REFRESH)


def _stage_group(sigs: list[Signature], run_id: int, names: list[str]) -> Signature:
    closer = tasks.close_stages.si(run_id=run_id, names=names)
    return chord(sigs, closer) if sigs else closer


def _anilist_fetches(run: IngestRun) -> list[Signature]:
    p = run.params
    if run.mode == RunMode.REFRESH:
        return [tasks.fetch_anilist_ids.si(run_id=run.id, ids=p["anilist_ids"])]
    if run.mode == RunMode.INCREMENTAL:
        return [
            tasks.fetch_anilist_updated.si(run_id=run.id),
            tasks.fetch_anilist_airing.si(run_id=run.id),
        ]
    if limit := p.get("limit"):
        pages = math.ceil(limit / settings.ANILIST_PER_PAGE)
        return [
            tasks.fetch_anilist_popular.si(run_id=run.id, page=n, limit=limit)
            for n in range(1, pages + 1)
        ]
    return [
        tasks.fetch_anilist_range.si(run_id=run.id, start=r.start, end=r.end)
        for r in anilist.id_ranges(settings.ANILIST_ID_RANGE)
    ]


def build(run: IngestRun) -> Signature:
    rid = run.id
    sources = run.params.get("sources") or list(ALL_SOURCES)
    steps: list[Signature] = [tasks.start_run.si(run_id=rid)]

    if run.mode in FETCH_MODES:
        steps.append(
            _stage_group(
                _anilist_fetches(run)
                if "anilist" in sources or run.mode == RunMode.REFRESH
                else [],
                rid,
                ["fetch_anilist"],
            )
        )
        steps.append(tasks.map_ids.si(run_id=rid))
        secondary: list[Signature] = []
        if "shikimori" in sources:
            secondary += [
                tasks.fetch_shikimori.si(run_id=rid, shard=i, shards=SECONDARY_SHARDS)
                for i in range(SECONDARY_SHARDS)
            ]
        if "annict" in sources:
            secondary += [
                tasks.fetch_annict.si(run_id=rid, shard=i, shards=SECONDARY_SHARDS)
                for i in range(SECONDARY_SHARDS)
            ]
        steps.append(_stage_group(secondary, rid, ["fetch_shikimori", "fetch_annict"]))

    if run.mode in (*FETCH_MODES, RunMode.MERGE):
        force = bool(run.params.get("force")) or run.mode == RunMode.REFRESH
        steps.append(
            _stage_group(
                [
                    tasks.merge_write.si(run_id=rid, shard=i, shards=MERGE_SHARDS, force=force)
                    for i in range(MERGE_SHARDS)
                ],
                rid,
                ["merge_write"],
            )
        )
        steps.append(tasks.link_relations.si(run_id=rid))
        if run.mode == RunMode.FULL:
            steps.append(tasks.mark_removed.si(run_id=rid))

    if "index" in runs.STAGES[run.mode]:
        steps.append(tasks.index_elasticsearch.si(run_id=rid))
    if "pools" in runs.STAGES[run.mode]:
        steps.append(tasks.rebuild_pools.si(run_id=rid))
    steps.append(tasks.finish_run.si(run_id=rid))
    return chain(*steps)


def start(mode: str, params: dict[str, Any] | None = None) -> IngestRun:
    """Create the run (taking the lock) and enqueue its canvas."""
    run = runs.create_run(mode, params or {})
    try:
        build(run).apply_async()
    except Exception as exc:
        runs.fail(run.id, None, f"could not enqueue: {exc!r}")
        raise
    return run
