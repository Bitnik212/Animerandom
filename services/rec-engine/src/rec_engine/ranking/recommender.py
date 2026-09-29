"""The ranking pipeline: candidates → blend → filters → reasons."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from rec_engine.cf.als import AlsModel
from rec_engine.cf.matrix import user_confidences
from rec_engine.config import settings
from rec_engine.db import AnimeMeta, Database, UserData
from rec_engine.ranking import blend as blending
from rec_engine.ranking import candidates as cand
from rec_engine.ranking import filters, reasons
from rec_engine.ranking.candidates import Candidate, CandidateSet

SEQUEL_WINDOW = 3  # sequel ordering and diversity look at limit × this many ranked items


@dataclass
class Ranked:
    anime_id: int
    score: float
    reason: dict[str, Any]
    genres: list[str] = field(default_factory=list)


class Recommender:
    def __init__(
        self,
        db: Database,
        model: Callable[[], AlsModel | None],
        embedding_model: str | None = None,
    ) -> None:
        self.db = db
        self.model = model
        self.embedding_model = embedding_model or settings().embedding_model
        self._genre_sizes: dict[str, int] | None = None

    def genre_sizes(self) -> dict[str, int]:
        if self._genre_sizes is None:
            self._genre_sizes = self.db.genre_sizes()
        return self._genre_sizes

    # --- Recommendations ----------------------------------------------------------------

    def recommend(self, user: UserData, limit: int) -> list[Ranked]:
        confidences = user_confidences(user.anime, user.not_interested)
        if not confidences:
            return self.popular(user, limit)

        liked = (
            user.top_rated(cand.CONTENT_SEEDS)
            or sorted(confidences, key=confidences.__getitem__, reverse=True)[: cand.CONTENT_SEEDS]
        )
        model = self.model()
        cf_enabled = (
            model is not None
            and model.n_users >= settings().min_users_for_cf
            and len(confidences) >= cand.MIN_CF_INTERACTIONS
        )
        pool = CandidateSet(user.excluded)

        # 1. Content: nearest neighbours of the user's liked anime.
        liked_vectors = self.db.embeddings(self.embedding_model, liked)
        for anime_id in liked:
            vector = liked_vectors.get(anime_id)
            if vector is None:
                continue
            for neighbor, _ in self.db.neighbors(
                self.embedding_model, vector, cand.NEIGHBORS_PER_SEED, user.excluded
            ):
                pool.add(neighbor)

        # 2. Collaborative filtering.
        cf_scores = model.scores(confidences) if cf_enabled and model else {}
        top_cf = sorted(
            (a for a in cf_scores if a not in user.excluded), key=lambda a: -cf_scores[a]
        )
        for anime_id in top_cf[: cand.CF_CANDIDATES]:
            pool.add(anime_id)

        # 3. Popular in the user's liked genres.
        liked_meta = self.db.anime_meta(liked)
        genres = cand.liked_genres([m.genres for m in liked_meta.values()])
        for anime_id, genre in self.db.popular(cand.GENRE_CANDIDATES, genres):
            c = pool.add(anime_id)
            if c and c.via_genre is None:
                c.via_genre = genre

        candidates = pool.values()
        meta = self.db.anime_meta([c.anime_id for c in candidates])
        candidates = [c for c in candidates if c.anime_id in meta]
        self._content_scores(candidates, liked_vectors)
        for c in candidates:
            c.cf = cf_scores.get(c.anime_id, 0.0)
            c.popularity = math.log1p(meta[c.anime_id].popularity)

        w = blending.weights(user.rated_or_completed, cf_enabled)
        ranked = blending.blend(candidates, w)
        by_id = {c.anime_id: c for c in ranked}

        def build(shown: int, original: int) -> Ranked:
            c = by_id[original]
            genres_of = meta[shown].genres if shown in meta else []
            return Ranked(shown, round(c.score, 4), reasons.reason(c, genres_of, genres), genres_of)

        return self._finish(user, [c.anime_id for c in ranked], meta, limit, build)

    def _content_scores(self, candidates: list[Candidate], liked: dict[int, np.ndarray]) -> None:
        if not liked or not candidates:
            return
        vectors = self.db.embeddings(self.embedding_model, [c.anime_id for c in candidates])
        liked_ids = list(liked)
        seeds = _unit(np.stack([liked[a] for a in liked_ids]))
        for c in candidates:
            v = vectors.get(c.anime_id)
            if v is None:
                continue
            sims = seeds @ _unit(v[None, :])[0]
            best = int(np.argmax(sims))
            c.content, c.content_from = float(sims[best]), liked_ids[best]

    def popular(self, user: UserData, limit: int) -> list[Ranked]:
        """Most popular titles the user doesn't have yet (users without data, unknown
        users, and the baseline in `rec evaluate`)."""
        found = [a for a, _ in self.db.popular(limit * SEQUEL_WINDOW * 2) if a not in user.excluded]
        meta = self.db.anime_meta(found)
        top = max((meta[a].popularity for a in found if a in meta), default=0) or 1

        def build(shown: int, original: int) -> Ranked:
            m = meta.get(original)
            score = math.log1p(m.popularity) / math.log1p(top) if m else 0.0
            genres = meta[shown].genres if shown in meta else []
            return Ranked(shown, round(score, 4), {"code": "popular"}, genres)

        return self._finish(user, [a for a in found if a in meta], meta, limit, build)

    def _finish(
        self,
        user: UserData,
        ranked_ids: list[int],
        meta: dict[int, AnimeMeta],
        limit: int,
        build: Callable[[int, int], Ranked],
    ) -> list[Ranked]:
        # 1–2. exclusions and adult titles
        adult = {a: m.is_adult for a, m in meta.items()}
        kept = filters.exclude(ranked_ids, user.excluded, adult, user.show_adult)
        window = kept[: limit * SEQUEL_WINDOW]

        # 3. sequel ordering over the prequel chains of the window
        prequels = self._prequel_closure(window)
        chain_meta = self.db.anime_meta({p for ps in prequels.values() for p in ps} - set(meta))
        meta.update(chain_meta)
        watched = {a for a, (s, _) in user.anime.items() if s in filters.WATCHED}

        def showable(anime_id: int) -> bool:
            m = meta.get(anime_id)
            return (
                m is not None
                and anime_id not in user.excluded
                and (user.show_adult or not m.is_adult)
            )

        ordered = filters.sequel_order(window, prequels, watched, showable)
        items = [build(shown, original) for shown, original in ordered]

        # 4. diversity
        sizes = self.genre_sizes()
        return filters.diversify(items, lambda r: filters.primary_genre(r.genres, sizes), limit)

    def _prequel_closure(self, ids: Iterable[int]) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        frontier = set(ids)
        for _ in range(filters.MAX_CHAIN):
            frontier -= set(out)
            if not frontier:
                break
            found = self.db.prequels(frontier)
            for a in frontier:
                out[a] = found.get(a, [])
            frontier = {p for ps in found.values() for p in ps}
        return out

    # --- Similar ------------------------------------------------------------------------------

    def similar(
        self, anime_id: int, limit: int, include_adult: bool = False
    ) -> list[tuple[int, float]]:
        vectors = self.db.embeddings(self.embedding_model, [anime_id])
        vector = vectors.get(anime_id)
        if vector is None:
            return []
        exclude = {anime_id} | self.db.related(anime_id)
        found = self.db.neighbors(self.embedding_model, vector, limit * 2, exclude)
        meta = self.db.anime_meta([a for a, _ in found])
        return [
            (a, round(sim, 4))
            for a, sim in found
            if a in meta and (include_adult or not meta[a].is_adult)
        ][:limit]


def _unit(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return np.asarray(x / np.where(norms == 0, 1, norms), dtype=np.float64)
