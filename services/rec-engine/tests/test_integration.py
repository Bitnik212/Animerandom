"""Against Postgres + pgvector: migrations, embedding, endpoints, training, evaluation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from rec_engine import jobs, scheduler
from rec_engine.app import create_app
from rec_engine.content import text as content_text
from rec_engine.db import Database
from tests.conftest import (
    ADULT,
    CHAIN,
    GENRE_LIST,
    REMOVED,
    HashEmbedder,
    add_user,
    anime_id,
    feedback,
    rate,
    user_uuid,
)


@pytest.fixture
def client(db: Database) -> Iterator[TestClient]:
    with TestClient(create_app(db, migrate=False, nightly=False)) as c:
        yield c


def ids(response: Any) -> list[int]:
    return [item["anime_id"] for item in response.json()["items"]]


# --- Schema and embedding -----------------------------------------------------------------


def test_migrations_live_in_rec(db):
    with db.connect() as conn:
        tables = set(
            conn.execute(
                text(
                    "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                    "WHERE table_schema = 'rec'"
                )
            ).scalars()
        )
        index = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'anime_embedding_hnsw'")
        ).scalar()
    assert tables == {"rec.anime_embedding", "rec.rec_model", "rec.alembic_version"}
    assert "hnsw" in index and "vector_cosine_ops" in index


SET_SYNOPSIS = text(
    "UPDATE catalog.anime_localization SET synopsis = :s WHERE anime_id = 5 AND locale = 'en'"
)


def test_embedding_text_and_incremental_runs(db):
    item = next(i for i in db.embedding_inputs() if i.anime_id == 1)
    assert content_text.build(item) == (
        "Action Tale 1. Genres: Action. Studio: Studio One. "
        "A story about battle sword fight warrior army battle sword fight warrior army number1."
    )
    first = jobs.embed_job(db, HashEmbedder())
    assert first == {"total": 61, "embedded": 61, "unchanged": 0, "removed": 0}  # REMOVED skipped
    assert jobs.embed_job(db, HashEmbedder())["embedded"] == 0

    original = next(i for i in db.embedding_inputs() if i.anime_id == 5).synopsis_en
    with db.engine.begin() as conn:
        conn.execute(SET_SYNOPSIS, {"s": "A new synopsis."})
    try:
        assert jobs.embed_job(db, HashEmbedder())["embedded"] == 1
    finally:
        with db.engine.begin() as conn:
            conn.execute(SET_SYNOPSIS, {"s": original})


def test_embed_refuses_a_model_with_another_dimension(db):
    class Wide(HashEmbedder):
        dim = 768

    with pytest.raises(ValueError, match="768"):
        jobs.embed_job(db, Wide())


# --- Similar -------------------------------------------------------------------------------


def test_similar_stays_in_genre_and_skips_itself_and_relations(embedded, client):
    response = client.get("/v1/similar/1?limit=5")
    assert response.status_code == 200
    found = ids(response)
    assert len(found) == 5
    assert 1 not in found and 2 not in found  # itself and its sequel
    assert all(a <= 10 for a in found)  # all action
    assert REMOVED not in found
    scores = [i["score"] for i in response.json()["items"]]
    assert scores == sorted(scores, reverse=True)


def test_similar_hides_adult_unless_asked(embedded, client):
    assert ADULT not in ids(client.get("/v1/similar/11?limit=20"))
    assert ADULT in ids(client.get("/v1/similar/11?limit=20&includeAdult=true"))


def test_similar_unknown_anime_is_404(embedded, client):
    response = client.get("/v1/similar/999999")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "anime-not-found"


# --- Recommendations ---------------------------------------------------------------------------


def test_unknown_user_gets_popular_titles(embedded, client):
    response = client.get(f"/v1/recommendations/{user_uuid(1)}?limit=10")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 10
    assert {i["reason"]["code"] for i in body["items"]} == {"popular"}
    assert ADULT not in ids(response) and REMOVED not in ids(response)
    assert body["model"] == {"als": None, "embedding": HashEmbedder.name}


def test_content_recommendations_explain_themselves(embedded, client):
    uid = add_user(embedded, 1)
    for j in (4, 5, 6):
        rate(embedded, uid, anime_id("sci-fi", j), score=9)
    feedback(embedded, uid, anime_id("sci-fi", 7))
    response = client.get(f"/v1/recommendations/{uid}?limit=6")
    items = response.json()["items"]
    found = [i["anime_id"] for i in items]
    sci_fi = set(range(21, 31))
    assert len(found) == 6
    assert not {25, 26, 27, 28} & set(found)  # rated and not_interested are excluded
    assert len([a for a in found if a in sci_fi]) >= 3
    similar = [i for i in items if i["reason"]["code"] == "similar_to"]
    assert similar and all(i["reason"]["anime_id"] in {25, 26, 27} for i in similar)


def test_sequel_is_replaced_by_the_first_unwatched_entry(embedded, client):
    uid = add_user(embedded, 2)
    for j in (5, 6, 7, 8, 9):
        rate(embedded, uid, anime_id("action", j), score=10)
    found = ids(client.get(f"/v1/recommendations/{uid}?limit=10"))
    assert 1 in found and 2 not in found and 3 not in found

    rate(embedded, uid, 1, score=9)  # watched season 1: season 2 is next, never season 3
    found = ids(client.get(f"/v1/recommendations/{uid}?limit=10"))
    assert 2 in found and 3 not in found


def test_planned_prequel_drops_the_sequel(embedded, client):
    uid = add_user(embedded, 3)
    for j in (5, 6, 7, 8, 9):
        rate(embedded, uid, anime_id("action", j), score=10)
    rate(embedded, uid, 1, status="planned")
    found = ids(client.get(f"/v1/recommendations/{uid}?limit=10"))
    assert not set(CHAIN) & set(found)


def test_adult_titles_only_for_opted_in_users(embedded, client):
    shy, bold = add_user(embedded, 4), add_user(embedded, 5, show_adult=True)
    for uid in (shy, bold):
        for j in range(3):
            rate(embedded, uid, anime_id("romance", j), score=9)
    assert ADULT not in ids(client.get(f"/v1/recommendations/{shy}?limit=20"))
    assert ADULT in ids(client.get(f"/v1/recommendations/{bold}?limit=20"))


def test_diversity_limits_one_genre_per_window(embedded, client):
    uid = add_user(embedded, 6)
    for genre in ("horror", "sports"):
        for j in range(3):
            rate(embedded, uid, anime_id(genre, j), score=9)
    items = client.get(f"/v1/recommendations/{uid}?limit=10").json()["items"]
    genres = [GENRE_LIST[(i["anime_id"] - 1) // 10] for i in items if i["anime_id"] <= 60]
    assert all(genres.count(g) <= 3 for g in set(genres))


def test_invalid_input_is_problem_json(client):
    response = client.get("/v1/recommendations/not-a-uuid")
    assert response.status_code == 422 and response.json()["type"] == "invalid-request"
    assert client.get(f"/v1/recommendations/{user_uuid(1)}?limit=0").status_code == 422


# --- Training, CF, evaluation --------------------------------------------------------------------


def test_train_needs_enough_users(embedded, settings_env):
    uid = add_user(embedded, 7)
    rate(embedded, uid, 1, score=9)
    assert jobs.train_job(embedded) == {"trained": False, "users_with_ratings": 1}
    assert embedded.active_model("als") is None


def test_train_activates_a_model_and_cf_joins_the_blend(embedded, community, client):
    result = jobs.train_job(embedded, factors=16, iterations=10)
    assert result["trained"] and result["users"] == 80
    model_id, path, _ = embedded.active_model("als")
    assert Path(path).exists() and model_id == result["id"]

    # A heavy user: CF weight is at its maximum, so some items come from similar users.
    uid = community[0]  # an action fan
    body = client.get(f"/v1/recommendations/{uid}?limit=10").json()
    assert body["model"]["als"] == result["model"]
    assert {i["reason"]["code"] for i in body["items"]} & {"liked_by_similar_users", "similar_to"}
    assert client.get("/healthz").json()["als_model"] == result["model"]

    # A second training run replaces the active model.
    second = jobs.train_job(embedded, factors=16, iterations=10)
    assert embedded.active_model("als")[0] == second["id"]


def test_new_user_gets_cf_scores_without_retraining(embedded, community, client):
    jobs.train_job(embedded, factors=16, iterations=10)
    uid = add_user(embedded, 8)
    for j in range(4):
        rate(embedded, uid, anime_id("comedy", j), score=10)
    found = ids(client.get(f"/v1/recommendations/{uid}?limit=5"))
    assert sum(1 for a in found if 31 <= a <= 40) >= 3  # stays with comedy


def test_evaluate_beats_popularity(embedded, community):
    metrics = jobs.evaluate_job(embedded, factors=16, iterations=10)
    assert metrics["cf_enabled"] and metrics["users"] > 30
    assert metrics["recall@20"] > metrics["popular_recall@20"]
    assert metrics["ndcg@20"] > metrics["popular_ndcg@20"]


def test_nightly_runs_once_under_the_advisory_lock(embedded, community, monkeypatch):
    monkeypatch.setattr(jobs, "embed_job", lambda db: jobs.embed.run(db, HashEmbedder()).__dict__)
    with embedded.engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": scheduler.LOCK_KEY})
        assert scheduler.run_once(embedded) is False  # another replica is running it
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": scheduler.LOCK_KEY})
        holder.commit()
    assert scheduler.run_once(embedded) is True
    assert embedded.active_model("als") is not None


def test_next_nightly_run():
    from datetime import UTC, datetime

    now = datetime(2026, 9, 29, 5, 0, tzinfo=UTC)
    assert scheduler.next_run("04:30", now) == datetime(2026, 9, 30, 4, 30, tzinfo=UTC)
    assert scheduler.next_run("06:00", now) == datetime(2026, 9, 29, 6, 0, tzinfo=UTC)


@pytest.fixture
def settings_env() -> None:
    """Placeholder so tests read clearly; settings come from the session environment."""


def test_missing_app_schema_means_unknown_users(embedded, client):
    with embedded.engine.begin() as conn:
        conn.execute(text("ALTER SCHEMA app RENAME TO app_later"))
    try:
        response = client.get(f"/v1/recommendations/{user_uuid(9)}?limit=3")
        assert response.status_code == 200
        assert {i["reason"]["code"] for i in response.json()["items"]} == {"popular"}
        assert jobs.train_job(embedded) == {"trained": False, "users_with_ratings": 0}
    finally:
        with embedded.engine.begin() as conn:
            conn.execute(text("ALTER SCHEMA app_later RENAME TO app"))


def test_model_store_picks_up_a_new_active_model(embedded, community):
    from rec_engine.cf.als import ModelStore

    store = ModelStore(embedded, reload_seconds=0)
    assert store.get() is None
    trained = jobs.train_job(embedded, factors=8, iterations=3)
    assert store.get().name == trained["model"]


def test_model_reload_does_not_block_other_requests(embedded, community):
    import threading
    import time

    from rec_engine.cf.als import ModelStore

    jobs.train_job(embedded, factors=8, iterations=3)
    store = ModelStore(embedded, reload_seconds=3600)
    model = store.get()
    slow_started = threading.Event()
    original = embedded.active_model

    def slow_active_model(kind):
        slow_started.set()
        time.sleep(1.0)
        return original(kind)

    embedded.active_model = slow_active_model  # type: ignore[method-assign]
    store.reload_seconds = 0
    reloader = threading.Thread(target=store.get)
    reloader.start()
    assert slow_started.wait(2)
    store.reload_seconds = 3600  # the next caller isn't due; it must not wait for the reload
    began = time.monotonic()
    assert store.get() is model
    assert time.monotonic() - began < 0.5
    reloader.join()
