"""User × anime confidence matrix for implicit-feedback ALS.

| Signal                 | Confidence                                  |
|------------------------|---------------------------------------------|
| Rated 6–10             | 1 + 4 × (score − 5) / 5  (6 → 1.8, 10 → 5)  |
| Rated 1–5              | nothing (no positive signal)                |
| Completed, no rating   | 2                                           |
| Watching               | 1.5                                         |
| Planned                | 0.5                                         |
| Dropped, not interested| excluded; used only as filters              |
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix

from rec_engine.db import Interaction

STATUS_CONFIDENCE = {"completed": 2.0, "watching": 1.5, "planned": 0.5}


def confidence(status: str, score: int | None) -> float | None:
    if status == "dropped":
        return None
    if score is not None:
        return 1 + 4 * (score - 5) / 5 if score > 5 else None
    return STATUS_CONFIDENCE.get(status)


def user_confidences(
    anime: Mapping[int, tuple[str, int | None]], not_interested: Iterable[int] = ()
) -> dict[int, float]:
    blocked = set(not_interested)
    out = {}
    for anime_id, (status, score) in anime.items():
        c = confidence(status, score)
        if c is not None and anime_id not in blocked:
            out[anime_id] = c
    return out


@dataclass
class Matrix:
    users: list[str]
    items: list[int]
    values: csr_matrix  # users × items, float32

    @property
    def item_index(self) -> dict[int, int]:
        return {a: i for i, a in enumerate(self.items)}


def build(interactions: Iterable[Interaction], blocked: set[tuple[str, int]]) -> Matrix:
    by_user: dict[str, dict[int, float]] = {}
    for it in interactions:
        c = confidence(it.status, it.score)
        if c is not None and (it.user_id, it.anime_id) not in blocked:
            by_user.setdefault(it.user_id, {})[it.anime_id] = c
    users = sorted(by_user)
    items = sorted({a for row in by_user.values() for a in row})
    index = {a: i for i, a in enumerate(items)}
    rows, cols, vals = [], [], []
    for u, user in enumerate(users):
        for anime_id, c in by_user[user].items():
            rows.append(u)
            cols.append(index[anime_id])
            vals.append(c)
    values = csr_matrix(
        (np.asarray(vals, dtype=np.float32), (rows, cols)), shape=(len(users), len(items))
    )
    return Matrix(users, items, values)
