"""FastAPI app. Internal: only the API service calls it."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rec_engine import jobs
from rec_engine.cf.als import ModelStore
from rec_engine.config import settings
from rec_engine.db import Database
from rec_engine.models import (
    Health,
    ModelInfo,
    Reason,
    RecItem,
    RecResponse,
    SimilarItem,
    SimilarResponse,
)
from rec_engine.ranking.recommender import Recommender
from rec_engine.scheduler import Nightly

log = logging.getLogger(__name__)


class Problem(Exception):
    def __init__(self, type_: str, title: str, status: int, detail: str = "") -> None:
        super().__init__(detail or title)
        self.type, self.title, self.status, self.detail = type_, title, status, detail


def problem_response(type_: str, title: str, status: int, detail: str = "") -> JSONResponse:
    return JSONResponse(
        {"type": type_, "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
    )


def create_app(
    db: Database | None = None, *, migrate: bool = True, nightly: bool = True
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = db or Database()
        if migrate:
            jobs.migrate()
        store = ModelStore(database)
        app.state.db = database
        app.state.models = store
        app.state.recommender = Recommender(database, store.get)
        worker = (
            Nightly(database, settings().nightly_at) if nightly and settings().nightly_at else None
        )
        if worker:
            worker.start()
        yield
        if worker:
            worker.stop()

    app = FastAPI(title="Rec engine", version="1", lifespan=lifespan)

    @app.exception_handler(Problem)
    async def on_problem(request: Request, exc: Problem) -> JSONResponse:
        return problem_response(exc.type, exc.title, exc.status, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response("invalid-request", "Invalid request", 422, str(exc.errors())[:1000])

    @app.get(
        "/v1/recommendations/{user_id}",
        response_model=RecResponse,
    )
    def recommendations(
        request: Request, user_id: UUID, limit: Annotated[int, Query(ge=1, le=100)] = 20
    ) -> RecResponse:
        state: Any = request.app.state
        user = state.db.user(str(user_id))
        ranked = state.recommender.recommend(user, limit)
        return RecResponse(
            items=[
                RecItem(anime_id=r.anime_id, score=r.score, reason=Reason(**r.reason))
                for r in ranked
            ],
            model=ModelInfo(als=state.models.info()["als"], embedding=settings().embedding_model),
        )

    @app.get("/v1/similar/{anime_id}", response_model=SimilarResponse)
    def similar(
        request: Request,
        anime_id: int,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        include_adult: Annotated[bool, Query(alias="includeAdult")] = False,
    ) -> SimilarResponse:
        state: Any = request.app.state
        if not state.db.anime_exists(anime_id):
            raise Problem("anime-not-found", "Anime not found", 404, f"No anime with id {anime_id}")
        found = state.recommender.similar(anime_id, limit, include_adult)
        return SimilarResponse(items=[SimilarItem(anime_id=a, score=s) for a, s in found])

    @app.get("/healthz", response_model=Health)
    def healthz(request: Request) -> Health:
        return Health(
            status="ok",
            als_model=request.app.state.models.info()["als"],
            embedding_model=settings().embedding_model,
        )

    return app


app = create_app()
