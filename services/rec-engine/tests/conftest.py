"""Integration setup: a fresh database with pgvector, the ingest worker's catalog
contract, the API's `app` migrations, and this service's migrations.

Point REC_TEST_POSTGRES_URL at a server where the role can create databases and the
pgvector extension is installed (default: a local server on port 5433).
"""

from __future__ import annotations

import os
import random
import uuid
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from numpy.typing import NDArray
from sqlalchemy import text

from rec_engine import config, jobs
from rec_engine.db import Database, engine

HERE = Path(__file__).parent
CONTRACT = HERE.parent.parent / "ingest-worker" / "contract" / "catalog-schema.sql"
# Schema `app` belongs to the API; its Flyway migrations are the source of truth.
APP_MIGRATIONS = HERE.parent.parent / "api" / "src" / "main" / "resources" / "db" / "migration"


def app_migrations() -> list[Path]:
    """V1__x.sql, V2__y.sql, ... in version order, as Flyway applies them."""
    return sorted(APP_MIGRATIONS.glob("V*__*.sql"), key=lambda p: int(p.name[1:].split("__")[0]))
ADMIN_URL = os.environ.get("REC_TEST_POSTGRES_URL", "postgresql://postgres@127.0.0.1:5433/postgres")

# Six genre clusters of ten anime each; the words make their synopses (and so their
# embeddings) cluster by genre.
GENRES = {
    "action": "battle sword fight warrior army",
    "romance": "love heart couple confession date",
    "sci-fi": "space robot future ship alien",
    "comedy": "funny gag laugh prank",
    "horror": "ghost curse blood fear night",
    "sports": "team match ball tournament coach",
}
GENRE_LIST = list(GENRES)
CHAIN = (1, 2, 3)  # action: 1 ← 2 ← 3 (2's prequel is 1, 3's prequel is 2)
ADULT = 61
REMOVED = 62


def anime_id(genre: str, j: int) -> int:
    return GENRE_LIST.index(genre) * 10 + j + 1


def user_uuid(i: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rec-test-user-{i}"))


class HashEmbedder:
    """Deterministic bag-of-words stand-in for the sentence-transformers model."""

    name = "test-hash-384"
    dim = 384

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, body in enumerate(texts):
            for token in body.lower().replace(".", " ").replace(",", " ").split():
                h = zlib.crc32(token.encode())
                out[row, h % self.dim] += 1.0 if (h >> 16) & 1 else -1.0
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    name = f"rec_test_{os.getpid()}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name}")
        admin.execute(f"CREATE DATABASE {name}")
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(CONTRACT.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        conn.execute("CREATE SCHEMA app")
        for migration in app_migrations():
            conn.execute(migration.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        _load_catalog(conn)

    os.environ.update(
        POSTGRES_URL=url,
        MODEL_DIR=str(tmp_path_factory.mktemp("models")),
        NIGHTLY_AT="",
        EMBEDDING_MODEL=HashEmbedder.name,
    )
    config.settings.cache_clear()
    jobs.migrate(config.settings().sqlalchemy_url)
    yield url
    engine.cache_clear()
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", [name]
        )
        admin.execute(f"DROP DATABASE IF EXISTS {name}")


@pytest.fixture
def db(database_url: str) -> Iterator[Database]:
    database = Database(engine(config.settings().sqlalchemy_url))
    yield database
    with database.engine.begin() as conn:
        conn.execute(text("TRUNCATE app.user_feedback, app.user_anime, app.app_user"))
        conn.execute(text("TRUNCATE rec.anime_embedding, rec.rec_model"))


@pytest.fixture
def embedded(db: Database) -> Database:
    jobs.embed_job(db, HashEmbedder())
    return db


def _load_catalog(conn: psycopg.Connection[Any]) -> None:
    now = "now()"
    for gi, genre in enumerate(GENRE_LIST):
        conn.execute("INSERT INTO catalog.genre (id, slug) VALUES (%s, %s)", [gi + 1, genre])
        conn.execute(
            "INSERT INTO catalog.genre_localization (genre_id, locale, name) VALUES (%s, 'en', %s)",
            [gi + 1, genre.title()],
        )
    conn.execute(
        "INSERT INTO catalog.studio (id, name, is_animation_studio) VALUES (1, 'Studio One', true)"
    )
    rows = []
    for genre, words in GENRES.items():
        for j in range(10):
            a = anime_id(genre, j)
            rows.append(
                (a, genre, words, 100_000 - j * 5_000 - GENRE_LIST.index(genre) * 100, False, None)
            )
    rows.append((ADULT, "romance", GENRES["romance"], 999_999, True, None))
    rows.append((REMOVED, "action", GENRES["action"], 500_000, False, "REMOVED"))
    for a, genre, words, popularity, adult, status in rows:
        conn.execute(
            f"INSERT INTO catalog.anime (id, anilist_id, format, status, season_year, episodes, "
            f"popularity, is_adult, created_at, updated_at) "
            f"VALUES (%s, %s, 'TV', %s, 2015, 12, %s, %s, {now}, {now})",
            [a, 100_000 + a, status or "FINISHED", popularity, adult],
        )
        conn.execute(
            "INSERT INTO catalog.anime_localization (anime_id, locale, title, synopsis, "
            "title_machine, synopsis_machine) VALUES (%s, 'en', %s, %s, false, false)",
            [a, f"{genre.title()} Tale {a}", f"A story about {words} {words} number{a}."],
        )
        conn.execute(
            "INSERT INTO catalog.anime_genre (anime_id, genre_id) VALUES (%s, %s)",
            [a, GENRE_LIST.index(genre) + 1],
        )
        conn.execute(
            "INSERT INTO catalog.anime_studio (anime_id, studio_id, is_main) VALUES (%s, 1, true)",
            [a],
        )
    for later, earlier in ((2, 1), (3, 2)):
        conn.execute(
            "INSERT INTO catalog.anime_relation (anime_id, related_id, kind) VALUES "
            "(%s, %s, 'PREQUEL'), (%s, %s, 'SEQUEL')",
            [later, earlier, earlier, later],
        )
    conn.execute("SELECT setval(pg_get_serial_sequence('catalog.anime', 'id'), 100)")


def add_user(db: Database, i: int, *, show_adult: bool = False) -> str:
    uid = user_uuid(i)
    with db.engine.begin() as conn:
        conn.execute(
            text("INSERT INTO app.app_user (id, show_adult) VALUES (CAST(:u AS uuid), :a)"),
            {"u": uid, "a": show_adult},
        )
    return uid


def rate(
    db: Database, uid: str, anime: int, status: str = "completed", score: int | None = None
) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app.user_anime (user_id, anime_id, status, score) "
                "VALUES (CAST(:u AS uuid), :a, :s, :sc)"
            ),
            {"u": uid, "a": anime, "s": status, "sc": score},
        )


def feedback(db: Database, uid: str, anime: int, kind: str = "not_interested") -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app.user_feedback (user_id, anime_id, kind) "
                "VALUES (CAST(:u AS uuid), :a, :k)"
            ),
            {"u": uid, "a": anime, "k": kind},
        )


@pytest.fixture
def community(db: Database) -> list[str]:
    """80 users, each a fan of one genre: 6 high ratings there, 2 low ones elsewhere."""
    rng = random.Random(7)
    uids = []
    for i in range(80):
        genre = GENRE_LIST[i % len(GENRE_LIST)]
        uid = add_user(db, 1000 + i)
        own = rng.sample(range(10), 6)
        for j in own:
            rate(db, uid, anime_id(genre, j), score=rng.randint(8, 10))
        others = [g for g in GENRE_LIST if g != genre]
        for g in rng.sample(others, 2):
            rate(db, uid, anime_id(g, rng.randrange(10)), score=rng.randint(2, 4))
        uids.append(uid)
    return uids
