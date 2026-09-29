"""Internal API at /api/v1: start and inspect runs. Nothing public calls it."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from celery import current_app
from django.db import connections
from django.http import HttpRequest, HttpResponse
from ninja import NinjaAPI, Query, Schema, Status
from ninja.errors import AuthenticationError, HttpError, ValidationError

from ingest.auth import KeycloakAdmin, Problem
from pipeline import orchestration, stages, vocab
from pipeline.http import SourceStats, bucket_for, redis_client
from pipeline.sources import annict
from runs import services as runs
from runs.models import IngestRun, RunMode, RunStatus

api = NinjaAPI(
    title="Ingest worker", version="1", auth=KeycloakAdmin(), urls_namespace="ingest-api"
)


def problem(
    request: HttpRequest, type_: str, title: str, status: int, detail: str = ""
) -> HttpResponse:
    response = api.create_response(
        request, {"type": type_, "title": title, "status": status, "detail": detail}, status=status
    )
    response["Content-Type"] = "application/problem+json"
    if status == 401:
        response["WWW-Authenticate"] = "Bearer"
    return response


@api.exception_handler(Problem)
def on_problem(request: HttpRequest, exc: Problem) -> HttpResponse:
    return problem(request, exc.type, exc.title, exc.status, exc.detail)


@api.exception_handler(AuthenticationError)
def on_auth_error(request: HttpRequest, exc: AuthenticationError) -> HttpResponse:
    return problem(request, "unauthorized", "Unauthorized", 401, "missing bearer token")


@api.exception_handler(ValidationError)
def on_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    return problem(request, "invalid-request", "Invalid request", 422, str(exc.errors)[:1000])


@api.exception_handler(HttpError)
def on_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    return problem(request, "http-error", str(exc), exc.status_code)


@api.exception_handler(runs.RunInProgress)
def on_run_in_progress(request: HttpRequest, exc: runs.RunInProgress) -> HttpResponse:
    return problem(
        request,
        "run-in-progress",
        "A run is already in progress",
        409,
        f"Run {exc.run_id} is active",
    )


def _subject(request: HttpRequest) -> str | None:
    """`sub` of the validated token (set by KeycloakAdmin), recorded on the runs it starts."""
    claims: dict[str, Any] = getattr(request, "auth", None) or {}
    return claims.get("sub")


def _run_or_404(run_id: int) -> IngestRun:
    run = IngestRun.objects.filter(id=run_id).first()
    if run is None:
        raise Problem("run-not-found", "Run not found", 404, f"No run with id {run_id}")
    return run


# --- Schemas -------------------------------------------------------------------------------

Source = Literal["anilist", "shikimori", "annict"]


class RunRequest(Schema):
    mode: Literal["full", "incremental"]
    limit: int | None = None
    sources: list[Source] | None = None


class RunCreated(Schema):
    runId: int


class StageOut(Schema):
    name: str
    status: str
    counts: dict[str, Any]
    startedAt: datetime | None
    finishedAt: datetime | None


class ItemErrorOut(Schema):
    stage: str
    source: str
    itemId: str
    kind: str
    message: str
    createdAt: datetime


class RunOut(Schema):
    id: int
    mode: str
    status: str
    params: dict[str, Any]
    error: str
    createdAt: datetime
    startedAt: datetime | None
    finishedAt: datetime | None


class RunDetail(RunOut):
    stages: list[StageOut]
    itemErrors: list[ItemErrorOut]
    itemErrorsTotal: int


def _run_out(run: IngestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "mode": run.mode,
        "status": run.status,
        "params": run.params,
        "error": run.error,
        "createdAt": run.created_at,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
    }


# --- Runs -----------------------------------------------------------------------------------


@api.post("/runs", response={202: RunCreated})
def start_run(request: HttpRequest, body: RunRequest) -> Status:
    if body.limit is not None and body.limit < 1:
        raise Problem("invalid-request", "Invalid request", 422, "limit must be positive")
    params: dict[str, Any] = {"requested_by": _subject(request)}
    if body.limit:
        params["limit"] = body.limit
    if body.sources:
        params["sources"] = body.sources
    run = orchestration.start(body.mode, params)
    return Status(202, {"runId": run.id})


@api.get("/runs", response=list[RunOut])
def list_runs(
    request: HttpRequest,
    status: RunStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    qs = IngestRun.objects.all()
    if status:
        qs = qs.filter(status=status)
    return [_run_out(r) for r in qs[:limit]]


@api.get("/runs/{run_id}", response=RunDetail)
def get_run(request: HttpRequest, run_id: int) -> dict:
    run = _run_or_404(run_id)
    errors = run.item_errors.all()
    return {
        **_run_out(run),
        "stages": [
            {
                "name": s.name,
                "status": s.status,
                "counts": s.counts,
                "startedAt": s.started_at,
                "finishedAt": s.finished_at,
            }
            for s in run.stages.all()
        ],
        "itemErrors": [
            {
                "stage": e.stage,
                "source": e.source,
                "itemId": e.item_id,
                "kind": e.kind,
                "message": e.message,
                "createdAt": e.created_at,
            }
            for e in errors[:500]
        ],
        "itemErrorsTotal": errors.count(),
    }


@api.post("/runs/{run_id}/cancel", response=RunOut)
def cancel_run(request: HttpRequest, run_id: int) -> dict:
    _run_or_404(run_id)
    return _run_out(runs.cancel(run_id))


# --- Anime ----------------------------------------------------------------------------------


@api.post("/anime/{anilist_id}/refresh", response={202: RunCreated})
def refresh_anime(request: HttpRequest, anilist_id: int) -> Status:
    run = orchestration.start(
        RunMode.REFRESH, {"anilist_ids": [anilist_id], "requested_by": _subject(request)}
    )  # type: ignore[attr-defined]
    return Status(202, {"runId": run.id})


@api.get("/anime/{anilist_id}/merge-preview")
def merge_preview(request: HttpRequest, anilist_id: int) -> dict:
    preview = stages.merge_preview(anilist_id)
    if preview is None:
        raise Problem(
            "anime-not-found", "Anime not found", 404, f"No stored AniList record {anilist_id}"
        )
    return preview


# --- Sources, index, pools, vocab -------------------------------------------------------------


@api.get("/sources")
def sources(request: HttpRequest) -> list[dict]:
    out = []
    for name in ("anilist", "shikimori", "annict"):
        stats = SourceStats(name).last_24h()
        bucket = bucket_for(name)
        requests = stats["requests"] or 0
        out.append(
            {
                "source": name,
                "enabled": annict.enabled() if name == "annict" else True,
                "lastSuccess": stats["last_success"],
                "requests24h": requests,
                "errorRate24h": round(stats["errors"] / requests, 4) if requests else 0.0,
                "throttled24h": stats["throttled"],
                "ratePerMinute": bucket.per_minute,
                "bucketLevel": bucket.level(),
            }
        )
    return out


@api.post("/index/rebuild", response={202: RunCreated})
def rebuild_index(request: HttpRequest) -> Status:
    return Status(202, {"runId": orchestration.start(RunMode.INDEX).id})


@api.post("/pools/rebuild", response={202: RunCreated})
def rebuild_pools(request: HttpRequest) -> Status:
    return Status(202, {"runId": orchestration.start(RunMode.POOLS).id})


@api.post("/vocab/sync")
def sync_vocab(request: HttpRequest) -> dict:
    return vocab.sync()


@api.get("/healthz", auth=None)
def healthz(request: HttpRequest) -> HttpResponse:
    checks: dict[str, str] = {}
    for alias in ("default", "catalog"):
        try:
            with connections[alias].cursor() as cursor:
                cursor.execute("SELECT 1")
            checks[f"postgres.{alias}"] = "ok"
        except Exception as exc:
            checks[f"postgres.{alias}"] = f"error: {exc.__class__.__name__}"
    try:
        redis_client().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc.__class__.__name__}"
    try:
        with current_app.connection_for_write() as conn:
            conn.ensure_connection(max_retries=1)
        checks["broker"] = "ok"
    except Exception as exc:
        checks["broker"] = f"error: {exc.__class__.__name__}"
    healthy = all(v == "ok" for v in checks.values())
    return api.create_response(
        request,
        {"status": "ok" if healthy else "error", "checks": checks},
        status=200 if healthy else 503,
    )
