"""ALS training, artifacts, and scoring.

Training uses `implicit`. Scoring recomputes the user's vector from their current
interactions with the same closed-form step ALS uses (implicit's `recalculate_user`),
so users who joined after training, and ratings made since, count right away:

    A = YᵀY + Σᵢ (cᵢ − 1) yᵢyᵢᵀ + λI,   b = Σᵢ cᵢ yᵢ,   x = A⁻¹b,   scores = Y x
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from threadpoolctl import threadpool_limits

from rec_engine.cf.matrix import Matrix
from rec_engine.config import settings
from rec_engine.db import Database

log = logging.getLogger(__name__)
KIND = "als"


@dataclass
class AlsModel:
    name: str
    item_ids: NDArray[np.int64]
    item_factors: NDArray[np.float32]  # items × factors
    regularization: float
    n_users: int

    def __post_init__(self) -> None:
        self.index = {int(a): i for i, a in enumerate(self.item_ids)}
        y = self.item_factors.astype(np.float64)
        self._yty = y.T @ y

    def user_vector(self, confidences: dict[int, float]) -> NDArray[np.float64] | None:
        known = [(self.index[a], c) for a, c in confidences.items() if a in self.index]
        if not known:
            return None
        y = self.item_factors.astype(np.float64)
        a = self._yty + self.regularization * np.eye(y.shape[1])
        b = np.zeros(y.shape[1])
        for i, c in known:
            yi = y[i]
            a += (c - 1.0) * np.outer(yi, yi)
            b += c * yi
        vec: NDArray[np.float64] = np.linalg.solve(a, b)
        return vec

    def scores(self, confidences: dict[int, float]) -> dict[int, float]:
        """ALS score for every item in the model, or {} when the user has no known items."""
        x = self.user_vector(confidences)
        if x is None:
            return {}
        raw = self.item_factors.astype(np.float64) @ x
        return {int(a): float(s) for a, s in zip(self.item_ids, raw, strict=True)}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            path,
            item_ids=self.item_ids,
            item_factors=self.item_factors,
            regularization=np.float64(self.regularization),
            n_users=np.int64(self.n_users),
            name=np.str_(self.name),
        )

    @classmethod
    def load(cls, path: Path) -> AlsModel:
        with np.load(path) as f:
            return cls(
                name=str(f["name"]),
                item_ids=f["item_ids"].astype(np.int64),
                item_factors=f["item_factors"].astype(np.float32),
                regularization=float(f["regularization"]),
                n_users=int(f["n_users"]),
            )


def fit(
    matrix: Matrix,
    *,
    factors: int | None = None,
    iterations: int | None = None,
    regularization: float | None = None,
    random_state: int = 42,
) -> AlsModel:
    from implicit.cpu.als import AlternatingLeastSquares

    s = settings()
    reg = s.als_regularization if regularization is None else regularization
    model = AlternatingLeastSquares(
        factors=factors or s.als_factors,
        iterations=iterations or s.als_iterations,
        regularization=reg,
        random_state=random_state,
    )
    # implicit parallelizes itself; a threaded BLAS underneath only slows it down.
    with threadpool_limits(1, "blas"):
        model.fit(matrix.values, show_progress=False)
    name = f"als-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    return AlsModel(
        name=name,
        item_ids=np.asarray(matrix.items, dtype=np.int64),
        item_factors=np.asarray(model.item_factors, dtype=np.float32),
        regularization=reg,
        n_users=len(matrix.users),
    )


def users_with_ratings(matrix: Matrix) -> int:
    return int((matrix.values.getnnz(axis=1) > 0).sum())


class ModelStore:
    """The active ALS model, re-read from rec.rec_model at most every reload interval."""

    def __init__(self, db: Database, reload_seconds: int | None = None) -> None:
        self.db = db
        self.reload_seconds = (
            settings().model_reload_seconds if reload_seconds is None else reload_seconds
        )
        self._lock = threading.Lock()
        self._model: AlsModel | None = None
        self._model_id: int | None = None
        self._checked = 0.0

    def get(self) -> AlsModel | None:
        """The current model. When a check is due, the one thread that claims it does the
        DB read and file load outside the lock; everyone else keeps serving the model
        they have meanwhile."""
        with self._lock:
            now = time.monotonic()
            due = self._checked == 0 or now - self._checked >= self.reload_seconds
            if due:
                self._checked = now  # claim the check
            loaded_id = self._model_id
        if due:
            self._refresh(loaded_id)
        with self._lock:
            return self._model

    def _refresh(self, loaded_id: int | None) -> None:
        try:
            active = self.db.active_model(KIND)
        except Exception:
            log.exception("could not read the active model; keeping the current one")
            return
        if active is None:
            with self._lock:
                self._model, self._model_id = None, None
            return
        model_id, path, _ = active
        if model_id == loaded_id:
            return
        try:
            model = AlsModel.load(Path(path))
        except OSError:
            log.exception("active model %s is unreadable; keeping the current one", path)
            return
        with self._lock:
            self._model, self._model_id = model, model_id
        log.info("loaded ALS model %s", model.name)

    def info(self) -> dict[str, Any]:
        model = self.get()
        return {"als": model.name if model else None}
