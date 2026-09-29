"""Engine and queries. Reads `catalog.*` and `app.*` (schema-qualified, read-only);
writes only schema `rec`."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pgvector.psycopg import register_vector
from psycopg import errors as pg_errors
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import ProgrammingError

from rec_engine.config import settings

Vector = NDArray[np.float32]
log = logging.getLogger(__name__)


def _app_schema_missing(exc: ProgrammingError) -> bool:
    """Schema `app` belongs to the API. Until its migrations have run, users simply
    don't exist yet; every other error is real."""
    if isinstance(exc.orig, pg_errors.UndefinedTable):
        log.warning("app tables are missing (has the API migrated?): treating users as unknown")
        return True
    return False


def _to_numpy(value: Any) -> Vector:
    """pgvector returns its own Vector type (or an ndarray in older versions)."""
    array = value.to_numpy() if hasattr(value, "to_numpy") else value
    return np.asarray(array, dtype=np.float32)


@cache
def engine(url: str | None = None) -> Engine:
    eng = create_engine(url or settings().sqlalchemy_url, pool_pre_ping=True, pool_size=5)

    @event.listens_for(eng, "connect")
    def _register(dbapi_connection: Any, _: Any) -> None:
        register_vector(dbapi_connection)

    return eng


@dataclass
class EmbeddingInput:
    anime_id: int
    title_en: str | None
    title_latn: str | None
    synopsis_en: str | None
    genres: list[str]
    tags: list[str]
    studio: str | None


@dataclass
class AnimeMeta:
    anime_id: int
    is_adult: bool
    popularity: int
    genres: list[str] = field(default_factory=list)


@dataclass
class UserData:
    """Everything the ranking needs to know about one user."""

    user_id: str
    known: bool = False
    show_adult: bool = False
    # anime_id -> (status, score); statuses: planned, watching, completed, dropped
    anime: dict[int, tuple[str, int | None]] = field(default_factory=dict)
    not_interested: set[int] = field(default_factory=set)

    @property
    def excluded(self) -> set[int]:
        return set(self.anime) | self.not_interested

    @property
    def rated_or_completed(self) -> int:
        return sum(1 for s, score in self.anime.values() if score is not None or s == "completed")

    def top_rated(self, n: int = 10) -> list[int]:
        """The user's best-liked anime: rated ones by score, then completed unrated."""
        rated = sorted(
            ((score, a) for a, (_, score) in self.anime.items() if score is not None and score > 5),
            reverse=True,
        )
        liked = [a for _, a in rated]
        liked += sorted(
            a for a, (s, score) in self.anime.items() if s == "completed" and score is None
        )
        return liked[:n]


@dataclass
class Interaction:
    user_id: str
    anime_id: int
    status: str
    score: int | None


class Database:
    """Query helpers over one SQLAlchemy engine."""

    def __init__(self, eng: Engine | None = None) -> None:
        self.engine = eng or engine()

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    # --- Content ------------------------------------------------------------------

    def embedding_inputs(self) -> list[EmbeddingInput]:
        sql = text(
            """
            SELECT a.id, en.title, latn.title, en.synopsis,
              COALESCE((SELECT array_agg(COALESCE(gl.name, g.slug) ORDER BY g.slug)
                        FROM catalog.anime_genre ag
                        JOIN catalog.genre g ON g.id = ag.genre_id
                        LEFT JOIN catalog.genre_localization gl
                          ON gl.genre_id = g.id AND gl.locale = 'en'
                        WHERE ag.anime_id = a.id), '{}'),
              COALESCE((SELECT array_agg(x.name ORDER BY x.rank DESC, x.name)
                        FROM (SELECT COALESCE(tl.name, t.slug) AS name, at.rank
                              FROM catalog.anime_tag at
                              JOIN catalog.tag t ON t.id = at.tag_id
                              LEFT JOIN catalog.tag_localization tl
                                ON tl.tag_id = t.id AND tl.locale = 'en'
                              WHERE at.anime_id = a.id AND NOT at.is_spoiler AND NOT t.is_spoiler
                              ORDER BY at.rank DESC, t.slug LIMIT 5) x), '{}'),
              (SELECT s.name FROM catalog.anime_studio st
               JOIN catalog.studio s ON s.id = st.studio_id
               WHERE st.anime_id = a.id AND st.is_main ORDER BY s.name LIMIT 1)
            FROM catalog.anime a
            LEFT JOIN catalog.anime_localization en ON en.anime_id = a.id AND en.locale = 'en'
            LEFT JOIN catalog.anime_localization latn
              ON latn.anime_id = a.id AND latn.locale = 'ja_latn'
            WHERE a.status IS DISTINCT FROM 'REMOVED'
            ORDER BY a.id
            """
        )
        with self.connect() as conn:
            return [
                EmbeddingInput(r[0], r[1], r[2], r[3], list(r[4]), list(r[5]), r[6])
                for r in conn.execute(sql)
            ]

    def embedding_hashes(self, model: str) -> dict[int, str]:
        sql = text("SELECT anime_id, source_hash FROM rec.anime_embedding WHERE model = :m")
        with self.connect() as conn:
            return {r[0]: r[1] for r in conn.execute(sql, {"m": model})}

    def upsert_embeddings(self, model: str, rows: Sequence[tuple[int, Vector, str]]) -> None:
        sql = text(
            """
            INSERT INTO rec.anime_embedding (anime_id, model, embedding, source_hash, updated_at)
            VALUES (:a, :m, :e, :h, now())
            ON CONFLICT (anime_id, model) DO UPDATE
              SET embedding = EXCLUDED.embedding, source_hash = EXCLUDED.source_hash,
                  updated_at = now()
            """
        )
        with self.engine.begin() as conn:
            conn.execute(sql, [{"a": a, "m": model, "e": e, "h": h} for a, e, h in rows])

    def delete_embeddings_except(self, model: str, keep: Iterable[int]) -> int:
        sql = text(
            "DELETE FROM rec.anime_embedding WHERE model = :m AND NOT (anime_id = ANY(:keep))"
        )
        with self.engine.begin() as conn:
            return conn.execute(sql, {"m": model, "keep": list(keep)}).rowcount

    def embeddings(self, model: str, anime_ids: Iterable[int]) -> dict[int, Vector]:
        sql = text(
            "SELECT anime_id, embedding FROM rec.anime_embedding "
            "WHERE model = :m AND anime_id = ANY(:ids)"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, {"m": model, "ids": list(anime_ids)})
            return {r[0]: _to_numpy(r[1]) for r in rows}

    def neighbors(
        self, model: str, vector: Vector, k: int, exclude: Iterable[int] = ()
    ) -> list[tuple[int, float]]:
        """Nearest anime by cosine similarity, skipping REMOVED and `exclude`."""
        sql = text(
            """
            SELECT e.anime_id, 1 - (e.embedding <=> :v) AS sim
            FROM rec.anime_embedding e
            JOIN catalog.anime a ON a.id = e.anime_id
            WHERE e.model = :m AND a.status IS DISTINCT FROM 'REMOVED'
              AND NOT (e.anime_id = ANY(:exclude))
            ORDER BY e.embedding <=> :v
            LIMIT :k
            """
        )
        with self.engine.begin() as conn:
            # Filters apply after the HNSW scan; widen it so k results survive them.
            conn.execute(text(f"SET LOCAL hnsw.ef_search = {max(100, 4 * k)}"))
            rows = conn.execute(sql, {"m": model, "v": vector, "k": k, "exclude": list(exclude)})
            return [(r[0], float(r[1])) for r in rows]

    # --- Catalog ------------------------------------------------------------------

    def anime_exists(self, anime_id: int) -> bool:
        sql = text("SELECT 1 FROM catalog.anime WHERE id = :id")
        with self.connect() as conn:
            return conn.execute(sql, {"id": anime_id}).first() is not None

    def anime_meta(self, anime_ids: Iterable[int]) -> dict[int, AnimeMeta]:
        ids = list(anime_ids)
        sql = text(
            """
            SELECT a.id, a.is_adult, COALESCE(a.popularity, 0),
              COALESCE((SELECT array_agg(g.slug ORDER BY g.slug) FROM catalog.anime_genre ag
                        JOIN catalog.genre g ON g.id = ag.genre_id WHERE ag.anime_id = a.id), '{}')
            FROM catalog.anime a
            WHERE a.id = ANY(:ids) AND a.status IS DISTINCT FROM 'REMOVED'
            """
        )
        with self.connect() as conn:
            return {
                r[0]: AnimeMeta(r[0], r[1], r[2], list(r[3]))
                for r in conn.execute(sql, {"ids": ids})
            }

    def related(self, anime_id: int) -> set[int]:
        """Direct relations in either direction (sequels, prequels, side stories, ...)."""
        sql = text(
            "SELECT related_id FROM catalog.anime_relation WHERE anime_id = :id "
            "UNION SELECT anime_id FROM catalog.anime_relation WHERE related_id = :id"
        )
        with self.connect() as conn:
            return {r[0] for r in conn.execute(sql, {"id": anime_id})}

    def prequels(self, anime_ids: Iterable[int]) -> dict[int, list[int]]:
        sql = text(
            "SELECT anime_id, related_id FROM catalog.anime_relation "
            "WHERE kind = 'PREQUEL' AND anime_id = ANY(:ids) ORDER BY related_id"
        )
        out: dict[int, list[int]] = defaultdict(list)
        with self.connect() as conn:
            for a, r in conn.execute(sql, {"ids": list(anime_ids)}):
                out[a].append(r)
        return dict(out)

    def popular(self, limit: int, genres: Sequence[str] = ()) -> list[tuple[int, str | None]]:
        """Most popular non-adult anime; with `genres`, the top `limit` of each genre.
        Returns (anime_id, genre it was found through)."""
        if not genres:
            sql = text(
                "SELECT id FROM catalog.anime WHERE NOT is_adult "
                "AND status IS DISTINCT FROM 'REMOVED' "
                "ORDER BY popularity DESC NULLS LAST, id LIMIT :n"
            )
            with self.connect() as conn:
                return [(r[0], None) for r in conn.execute(sql, {"n": limit})]
        sql = text(
            """
            SELECT id, slug FROM (
              SELECT a.id, g.slug, row_number() OVER (
                PARTITION BY g.slug ORDER BY a.popularity DESC NULLS LAST, a.id) AS rn
              FROM catalog.anime a
              JOIN catalog.anime_genre ag ON ag.anime_id = a.id
              JOIN catalog.genre g ON g.id = ag.genre_id
              WHERE g.slug = ANY(:genres) AND NOT a.is_adult
                AND a.status IS DISTINCT FROM 'REMOVED'
            ) ranked WHERE rn <= :n
            """
        )
        with self.connect() as conn:
            return [(r[0], r[1]) for r in conn.execute(sql, {"genres": list(genres), "n": limit})]

    def genre_sizes(self) -> dict[str, int]:
        sql = text(
            "SELECT g.slug, count(*) FROM catalog.anime_genre ag "
            "JOIN catalog.genre g ON g.id = ag.genre_id GROUP BY g.slug"
        )
        with self.connect() as conn:
            return {r[0]: r[1] for r in conn.execute(sql)}

    # --- Users ----------------------------------------------------------------------

    def user(self, user_id: str) -> UserData:
        try:
            return self._user(user_id)
        except ProgrammingError as exc:
            if _app_schema_missing(exc):
                return UserData(user_id)
            raise

    def _user(self, user_id: str) -> UserData:
        data = UserData(user_id)
        with self.connect() as conn:
            row = conn.execute(
                text("SELECT show_adult FROM app.app_user WHERE id = CAST(:u AS uuid)"),
                {"u": user_id},
            ).first()
            if row is not None:
                data.known, data.show_adult = True, bool(row[0])
            for anime_id, status, score in conn.execute(
                text(
                    "SELECT anime_id, status, score FROM app.user_anime "
                    "WHERE user_id = CAST(:u AS uuid)"
                ),
                {"u": user_id},
            ):
                data.anime[anime_id] = (status, score)
            data.not_interested = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT anime_id FROM app.user_feedback "
                        "WHERE user_id = CAST(:u AS uuid) AND kind = 'not_interested'"
                    ),
                    {"u": user_id},
                )
            }
        data.known = data.known or bool(data.anime)
        return data

    def interactions(self) -> tuple[list[Interaction], set[tuple[str, int]]]:
        """All watch statuses and ratings, plus (user, anime) pairs marked not_interested."""
        try:
            return self._interactions()
        except ProgrammingError as exc:
            if _app_schema_missing(exc):
                return [], set()
            raise

    def _interactions(self) -> tuple[list[Interaction], set[tuple[str, int]]]:
        with self.connect() as conn:
            rows = [
                Interaction(str(u), a, s, score)
                for u, a, s, score in conn.execute(
                    text("SELECT user_id, anime_id, status, score FROM app.user_anime")
                )
            ]
            blocked = {
                (str(u), a)
                for u, a in conn.execute(
                    text(
                        "SELECT user_id, anime_id FROM app.user_feedback "
                        "WHERE kind = 'not_interested'"
                    )
                )
            }
        return rows, blocked

    def users(self, user_ids: Iterable[str]) -> dict[str, bool]:
        """show_adult per known user."""
        sql = text("SELECT id::text, show_adult FROM app.app_user WHERE id::text = ANY(:ids)")
        with self.connect() as conn:
            return {r[0]: bool(r[1]) for r in conn.execute(sql, {"ids": list(user_ids)})}

    # --- Model registry -------------------------------------------------------------------

    def register_model(self, kind: str, path: str, metrics: dict[str, Any], activate: bool) -> int:
        with self.engine.begin() as conn:
            if activate:
                conn.execute(
                    text(
                        "UPDATE rec.rec_model SET is_active = false WHERE kind = :k AND is_active"
                    ),
                    {"k": kind},
                )
            row = conn.execute(
                text(
                    "INSERT INTO rec.rec_model (kind, artifact_path, metrics, is_active) "
                    "VALUES (:k, :p, CAST(:m AS jsonb), :a) RETURNING id"
                ),
                {"k": kind, "p": path, "m": json.dumps(metrics), "a": activate},
            ).one()
            return int(row[0])

    def active_model(self, kind: str) -> tuple[int, str, datetime] | None:
        sql = text(
            "SELECT id, artifact_path, trained_at FROM rec.rec_model WHERE kind = :k AND is_active"
        )
        with self.connect() as conn:
            row = conn.execute(sql, {"k": kind}).first()
            return (int(row[0]), str(row[1]), row[2]) if row else None
