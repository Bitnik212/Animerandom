"""Run lifecycle: the overlap lock, stage status, counters, and item errors."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone

from pipeline.http import redis_client
from runs.models import IngestRun, RunItemError, RunMode, RunStage, RunStatus

LOCK_KEY = "lock:ingest:run"
LOCK_TTL = 6 * 3600

STAGES: dict[str, tuple[str, ...]] = {
    RunMode.FULL: (
        "fetch_anilist",
        "map_ids",
        "fetch_shikimori",
        "fetch_annict",
        "merge_write",
        "link_relations",
        "mark_removed",
        "index",
        "pools",
    ),
    RunMode.INCREMENTAL: (
        "fetch_anilist",
        "map_ids",
        "fetch_shikimori",
        "fetch_annict",
        "merge_write",
        "link_relations",
        "index",
        "pools",
    ),
    RunMode.REFRESH: (
        "fetch_anilist",
        "map_ids",
        "fetch_shikimori",
        "fetch_annict",
        "merge_write",
        "link_relations",
        "index",
        "pools",
    ),
    RunMode.MERGE: ("merge_write", "link_relations", "index", "pools"),
    RunMode.INDEX: ("index",),
    RunMode.POOLS: ("pools",),
}


class RunInProgress(Exception):
    def __init__(self, run_id: int | None) -> None:
        super().__init__(f"run {run_id} is in progress")
        self.run_id = run_id


def active_run_id() -> int | None:
    value = redis_client().get(LOCK_KEY)
    return int(value) if value else None


def create_run(mode: str, params: dict | None = None) -> IngestRun:
    """Create a run and take the overlap lock. Raises RunInProgress if one is active."""
    run = IngestRun.objects.create(mode=mode, params=params or {})
    if not redis_client().set(LOCK_KEY, run.id, nx=True, ex=LOCK_TTL):
        other = active_run_id()
        run.delete()
        raise RunInProgress(other)
    RunStage.objects.bulk_create(
        [RunStage(run=run, name=name, position=i) for i, name in enumerate(STAGES[mode])]
    )
    return run


# Extends the lock only while it still belongs to this run.
_REFRESH_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


def refresh_lock(run_id: int) -> bool:
    """Push the lock's expiry out while the run is alive. LOCK_TTL then only bounds how
    long a dead run (crashed worker, lost tasks) can block new runs."""
    return bool(redis_client().eval(_REFRESH_LOCK, 1, LOCK_KEY, str(run_id), LOCK_TTL))


def release_lock(run_id: int) -> None:
    r = redis_client()
    if r.get(LOCK_KEY) == str(run_id):
        r.delete(LOCK_KEY)


def mark_running(run_id: int) -> None:
    IngestRun.objects.filter(id=run_id, status=RunStatus.PENDING).update(
        status=RunStatus.RUNNING, started_at=timezone.now()
    )


def should_stop(run_id: int) -> bool:
    run = IngestRun.objects.only("status", "cancel_requested").get(id=run_id)
    return run.cancel_requested or run.is_finished


def stage_started(run_id: int, name: str) -> None:
    RunStage.objects.filter(run_id=run_id, name=name, status=RunStatus.PENDING).update(
        status=RunStatus.RUNNING, started_at=timezone.now()
    )


def stage_done(run_id: int, names: Iterable[str]) -> None:
    now = timezone.now()
    for name in names:
        RunStage.objects.filter(run_id=run_id, name=name, status=RunStatus.PENDING).update(
            started_at=now
        )
        RunStage.objects.filter(
            run_id=run_id, name=name, status__in=[RunStatus.PENDING, RunStatus.RUNNING]
        ).update(status=RunStatus.DONE, finished_at=now)


def stage_count(run_id: int, name: str, **increments: int) -> None:
    if not any(increments.values()):
        return
    with transaction.atomic():
        stage = RunStage.objects.select_for_update().get(run_id=run_id, name=name)
        for key, value in increments.items():
            stage.counts[key] = stage.counts.get(key, 0) + value
        stage.save(update_fields=["counts"])


def stage_set(run_id: int, name: str, **values: object) -> None:
    with transaction.atomic():
        stage = RunStage.objects.select_for_update().get(run_id=run_id, name=name)
        stage.counts.update(values)
        stage.save(update_fields=["counts"])


def record_item(
    run_id: int,
    stage: str,
    message: str,
    *,
    source: str = "",
    item_id: str | int = "",
    kind: str = "error",
    unique: bool = False,
) -> None:
    """Record a failed item or logged conflict. With `unique`, a (stage, kind, item_id)
    already recorded on this run is not repeated."""
    lookup = {"run_id": run_id, "stage": stage, "kind": kind, "item_id": str(item_id)}
    if unique and RunItemError.objects.filter(**lookup).exists():
        return
    RunItemError.objects.create(**lookup, source=source, message=message[:2000])
    if kind == "error":
        stage_count(run_id, stage, failed=1)


def finish(run_id: int) -> None:
    now = timezone.now()
    IngestRun.objects.filter(id=run_id, status__in=[RunStatus.PENDING, RunStatus.RUNNING]).update(
        status=RunStatus.DONE, finished_at=now
    )
    release_lock(run_id)


def fail(run_id: int, stage: str | None, error: str) -> None:
    now = timezone.now()
    if stage:
        RunStage.objects.filter(run_id=run_id, name=stage).exclude(status=RunStatus.DONE).update(
            status=RunStatus.FAILED, finished_at=now
        )
    IngestRun.objects.filter(id=run_id, status__in=[RunStatus.PENDING, RunStatus.RUNNING]).update(
        status=RunStatus.FAILED, finished_at=now, error=error[:5000]
    )
    release_lock(run_id)


def cancel(run_id: int) -> IngestRun:
    """Stop the run: queued tasks see the flag and exit; finished stages stay."""
    now = timezone.now()
    run = IngestRun.objects.get(id=run_id)
    if run.is_finished:
        return run
    run.cancel_requested = True
    run.status = RunStatus.CANCELLED
    run.finished_at = now
    run.save(update_fields=["cancel_requested", "status", "finished_at"])
    run.stages.filter(status__in=[RunStatus.PENDING, RunStatus.RUNNING]).update(
        status=RunStatus.CANCELLED, finished_at=now
    )
    release_lock(run_id)
    return run
