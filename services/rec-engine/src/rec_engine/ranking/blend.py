"""Hybrid scoring.

    n      = rated or completed anime of the user
    w_cf   = min(1, n / 20) × 0.6        (0 when CF is disabled)
    w_pop  = 0.1
    w_cont = 1 − w_cf − w_pop
    score  = w_cont × content + w_cf × cf + w_pop × popularity

Each component is min-max normalized to 0–1 within the candidate set first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rec_engine.ranking.candidates import Candidate

W_POP = 0.1
CF_MAX = 0.6
CF_FULL_AT = 20


@dataclass(frozen=True)
class Weights:
    content: float
    cf: float
    popularity: float


def weights(n_rated_or_completed: int, cf_enabled: bool) -> Weights:
    w_cf = min(1.0, n_rated_or_completed / CF_FULL_AT) * CF_MAX if cf_enabled else 0.0
    return Weights(content=1.0 - w_cf - W_POP, cf=w_cf, popularity=W_POP)


def normalize(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def blend(candidates: list[Candidate], w: Weights) -> list[Candidate]:
    """Set `score` and the dominant `reason_component`; return sorted best first."""
    content = normalize([c.content for c in candidates])
    cf = normalize([c.cf for c in candidates])
    pop = normalize([c.popularity for c in candidates])
    for c, vc, vf, vp in zip(candidates, content, cf, pop, strict=True):
        parts = {"content": w.content * vc, "cf": w.cf * vf, "popularity": w.popularity * vp}
        c.score = sum(parts.values())
        c.reason_component = max(parts, key=lambda k: (parts[k], k == "popularity"))
    return sorted(candidates, key=lambda c: (-c.score, c.anime_id))
