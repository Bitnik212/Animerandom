"""Celery tasks: thin wrappers around `pipeline.stages`.

Every task takes keyword arguments only (run id, cursors, small scalars), checks
the run's cancel flag first, and is idempotent. Failure handling:

- RateLimited (no token soon, or HTTP 429): retry from the saved cursor after the
  wait; never counted against the attempt limit.
- TransientError (network, 5xx): exponential backoff with jitter, up to 6 attempts,
  then the rest of that task's work is recorded as a failed item.
- ItemFailed (other 4xx): the item is recorded and the task moves on.
- Anything else: the run is marked failed (PipelineTask.on_failure).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import Any

from celery import Task, shared_task
from celery.exceptions import Ignore
from django.db import InterfaceError, OperationalError

from pipeline import stages
from pipeline.http import ItemFailed, RateLimited, TransientError
from pipeline.idmap import arm
from runs import services as runs

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6


class PipelineTask(Task):
    stage: str | None = None
    acks_late = True
    reject_on_worker_lost = True
    # Retry limits are enforced here, not by Celery: rate-limit retries are unlimited
    # and transient errors count their own attempts (see _with_retries).
    max_retries = None

    def on_failure(
        self, exc: BaseException, task_id: str, args: Any, kwargs: Any, einfo: Any
    ) -> None:
        run_id = kwargs.get("run_id")
        if run_id is not None:
            runs.fail(run_id, self.stage, f"{self.name}: {type(exc).__name__}: {exc}")


def pipeline_task(stage: str | None = None, **options: Any) -> Callable:
    def decorator(fn: Callable) -> Task:
        task: Task = shared_task(base=PipelineTask, bind=True, **options)(fn)
        task.stage = stage  # type: ignore[attr-defined]
        return task

    return decorator


def _guard(run_id: int, stage: str | None) -> None:
    if runs.should_stop(run_id):
        raise Ignore()
    if stage:
        runs.stage_started(run_id, stage)


def backoff(attempt: int) -> float:
    return min(600.0, 5.0 * 2**attempt) * random.uniform(0.75, 1.25)


def _with_retries(
    task: Task, stage: str, run_id: int, cursor_key: str | None, call: Callable[..., None]
) -> None:
    """Run `call(on_progress)` and turn source errors into cursor-preserving retries."""
    kwargs = dict(task.request.kwargs or {})
    start_cursor = kwargs.get(cursor_key) if cursor_key else None
    progress = {"cursor": start_cursor}

    def on_progress(value: Any) -> None:
        progress["cursor"] = value

    attempt = int(kwargs.get("attempt", 0))
    try:
        call(on_progress)
    except RateLimited as exc:
        if cursor_key:
            kwargs[cursor_key] = progress["cursor"]
        raise task.retry(kwargs=kwargs, countdown=exc.wait) from exc
    except TransientError as exc:
        moved = cursor_key is not None and progress["cursor"] != start_cursor
        attempt = 0 if moved else attempt + 1
        if attempt >= MAX_ATTEMPTS:
            runs.record_item(
                run_id,
                stage,
                f"gave up after {MAX_ATTEMPTS} attempts: {exc}",
                item_id=f"{task.name.rsplit('.', 1)[-1]}:{progress['cursor']}",
            )
            return
        if cursor_key:
            kwargs[cursor_key] = progress["cursor"]
        kwargs["attempt"] = attempt
        raise task.retry(kwargs=kwargs, countdown=backoff(attempt)) from exc
    except ItemFailed as exc:
        runs.record_item(
            run_id, stage, str(exc), item_id=f"{task.name.rsplit('.', 1)[-1]}:{progress['cursor']}"
        )


# --- Run control -------------------------------------------------------------------------


@pipeline_task()
def start_run(self: Task, *, run_id: int) -> None:
    _guard(run_id, None)
    stages.start(run_id)


@pipeline_task()
def close_stages(self: Task, *, run_id: int, names: list[str]) -> None:
    _guard(run_id, None)
    runs.stage_done(run_id, names)


@pipeline_task()
def finish_run(self: Task, *, run_id: int) -> None:
    _guard(run_id, None)
    runs.finish(run_id)


# --- Fetch -------------------------------------------------------------------------------


@pipeline_task("fetch_anilist")
def fetch_anilist_range(
    self: Task,
    *,
    run_id: int,
    start: int,
    end: int | None,
    after_id: int | None = None,
    attempt: int = 0,
) -> None:
    _guard(run_id, "fetch_anilist")
    _with_retries(
        self,
        "fetch_anilist",
        run_id,
        "after_id",
        lambda p: stages.fetch_anilist_range(run_id, start, end, after_id, p),
    )


@pipeline_task("fetch_anilist")
def fetch_anilist_popular(
    self: Task, *, run_id: int, page: int, limit: int, attempt: int = 0
) -> None:
    _guard(run_id, "fetch_anilist")
    _with_retries(
        self,
        "fetch_anilist",
        run_id,
        None,
        lambda p: stages.fetch_anilist_popular(run_id, page, limit),
    )


@pipeline_task("fetch_anilist")
def fetch_anilist_updated(self: Task, *, run_id: int, page: int = 1, attempt: int = 0) -> None:
    _guard(run_id, "fetch_anilist")
    _with_retries(
        self,
        "fetch_anilist",
        run_id,
        "page",
        lambda p: stages.fetch_anilist_updated(run_id, page, p),
    )


@pipeline_task("fetch_anilist")
def fetch_anilist_airing(
    self: Task, *, run_id: int, after_id: int | None = None, attempt: int = 0
) -> None:
    _guard(run_id, "fetch_anilist")
    _with_retries(
        self,
        "fetch_anilist",
        run_id,
        "after_id",
        lambda p: stages.fetch_anilist_airing(run_id, after_id, p),
    )


@pipeline_task("fetch_anilist")
def fetch_anilist_ids(self: Task, *, run_id: int, ids: list[int], attempt: int = 0) -> None:
    _guard(run_id, "fetch_anilist")
    _with_retries(
        self, "fetch_anilist", run_id, "ids", lambda p: stages.fetch_anilist_ids(run_id, ids, p)
    )


@pipeline_task("map_ids")
def map_ids(self: Task, *, run_id: int) -> None:
    _guard(run_id, "map_ids")
    stages.map_ids(run_id)
    runs.stage_done(run_id, ["map_ids"])


@pipeline_task("fetch_shikimori")
def fetch_shikimori(
    self: Task, *, run_id: int, shard: int, shards: int, after: int | None = None, attempt: int = 0
) -> None:
    _guard(run_id, "fetch_shikimori")
    _with_retries(
        self,
        "fetch_shikimori",
        run_id,
        "after",
        lambda p: stages.fetch_shikimori(run_id, shard, shards, after, p),
    )


@pipeline_task("fetch_annict")
def fetch_annict(
    self: Task, *, run_id: int, shard: int, shards: int, after: int | None = None, attempt: int = 0
) -> None:
    _guard(run_id, "fetch_annict")
    _with_retries(
        self,
        "fetch_annict",
        run_id,
        "after",
        lambda p: stages.fetch_annict(run_id, shard, shards, after, p),
    )


# --- Merge and sinks -----------------------------------------------------------------------


@pipeline_task("merge_write")
def merge_write(
    self: Task,
    *,
    run_id: int,
    shard: int,
    shards: int,
    after: int | None = None,
    force: bool = False,
) -> None:
    _guard(run_id, "merge_write")
    progress = {"after": after}

    def on_progress(value: int) -> None:
        progress["after"] = value

    try:
        stages.merge_write(run_id, shard, shards, after, force, on_progress)
    except (OperationalError, InterfaceError) as exc:
        # Lost database connection: resume after the last committed batch. Other
        # exceptions are bugs and fail the run (PipelineTask.on_failure).
        if self.request.retries < 3:
            kwargs = {**(self.request.kwargs or {}), "after": progress["after"]}
            raise self.retry(
                kwargs=kwargs, countdown=backoff(self.request.retries), exc=exc
            ) from exc
        raise


@pipeline_task("link_relations")
def link_relations(self: Task, *, run_id: int) -> None:
    _guard(run_id, "link_relations")
    stages.link_relations(run_id)
    runs.stage_done(run_id, ["link_relations"])


@pipeline_task("mark_removed")
def mark_removed(self: Task, *, run_id: int) -> None:
    _guard(run_id, "mark_removed")
    stages.mark_removed(run_id)
    runs.stage_done(run_id, ["mark_removed"])


@pipeline_task("index")
def index_elasticsearch(self: Task, *, run_id: int) -> None:
    _guard(run_id, "index")
    stages.index(run_id)
    runs.stage_done(run_id, ["index"])


@pipeline_task("pools")
def rebuild_pools(self: Task, *, run_id: int) -> None:
    _guard(run_id, "pools")
    stages.pools(run_id)
    runs.stage_done(run_id, ["pools"])


# --- Scheduled entry points (django-celery-beat) -------------------------------------------


@shared_task
def scheduled_run(mode: str, **params: Any) -> int | None:
    from pipeline import orchestration

    try:
        return orchestration.start(mode, params).id
    except runs.RunInProgress as exc:
        log.warning("scheduled %s run skipped: run %s is in progress", mode, exc.run_id)
        return None


@shared_task
def refresh_arm() -> int:
    return arm.refresh()
