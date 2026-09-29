"""Offline jobs shared by the CLI and the nightly scheduler: embed, train, evaluate."""

from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from rec_engine.cf import als
from rec_engine.cf import matrix as cf_matrix
from rec_engine.config import settings
from rec_engine.content import embed
from rec_engine.content.embed import Embedder
from rec_engine.db import Database, Interaction, UserData
from rec_engine.ranking.recommender import Recommender

log = logging.getLogger(__name__)
SERVICE_ROOT = Path(__file__).resolve().parents[2]


def migrate(url: str | None = None) -> None:
    cfg = Config(str(SERVICE_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_ROOT / "migrations"))
    if url:
        cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def embed_job(
    db: Database, embedder: Embedder | None = None, force: bool = False
) -> dict[str, int]:
    return asdict(embed.run(db, embedder, force=force))


def users_with_ratings(interactions: list[Interaction]) -> int:
    return len({i.user_id for i in interactions if i.score is not None})


def train_job(db: Database, **fit_options: Any) -> dict[str, Any]:
    """Train ALS on all interactions and activate it. Below MIN_USERS_FOR_CF users with
    ratings nothing is trained and CF stays off."""
    interactions, blocked = db.interactions()
    rated_users = users_with_ratings(interactions)
    if rated_users < settings().min_users_for_cf:
        log.warning(
            "CF disabled: %d users with ratings (< %d)", rated_users, settings().min_users_for_cf
        )
        return {"trained": False, "users_with_ratings": rated_users}
    m = cf_matrix.build(interactions, blocked)
    model = als.fit(m, **fit_options)
    path = settings().model_dir / f"{model.name}.npz"
    model.save(path)
    metrics = {
        "users": len(m.users),
        "items": len(m.items),
        "interactions": int(m.values.nnz),
        "users_with_ratings": rated_users,
    }
    model_id = db.register_model(als.KIND, str(path), metrics, activate=True)
    return {"trained": True, "model": model.name, "id": model_id, **metrics}


def evaluate_job(
    db: Database, *, holdout: float = 0.2, k: int = 20, seed: int = 42, **fit_options: Any
) -> dict[str, Any]:
    """Hold out a random share of ratings ≥ 8, train on the rest, and measure how many
    come back in each user's top k (recall@k, nDCG@k). Also reports a popularity baseline."""
    interactions, blocked = db.interactions()
    positives = [i for i in interactions if i.score is not None and i.score >= 8]
    rng = random.Random(seed)
    held = {(i.user_id, i.anime_id) for i in rng.sample(positives, int(len(positives) * holdout))}
    train = [i for i in interactions if (i.user_id, i.anime_id) not in held]

    model = None
    if users_with_ratings(train) >= settings().min_users_for_cf:
        model = als.fit(cf_matrix.build(train, blocked), **fit_options)

    by_user: dict[str, list[Interaction]] = defaultdict(list)
    for i in train:
        by_user[i.user_id].append(i)
    targets: dict[str, set[int]] = defaultdict(set)
    for user_id, anime_id in held:
        targets[user_id].add(anime_id)
    adult = db.users(targets)

    results: dict[str, list[tuple[float, float]]] = {"hybrid": [], "popular": []}
    rec = Recommender(db, lambda: model)
    for name in ("hybrid", "popular"):
        for user_id, wanted in targets.items():
            # Both arms get the same history, so both exclude what the user already has;
            # the baseline then ranks by popularity alone.
            user = UserData(user_id, known=True, show_adult=adult.get(user_id, False))
            user.anime = {i.anime_id: (i.status, i.score) for i in by_user[user_id]}
            user.not_interested = {a for u, a in blocked if u == user_id}
            ranked = rec.recommend(user, k) if name == "hybrid" else rec.popular(user, k)
            got = [r.anime_id for r in ranked]
            results[name].append(_metrics(got, wanted, k))

    def mean(pairs: list[tuple[float, float]], idx: int) -> float:
        return round(sum(p[idx] for p in pairs) / len(pairs), 4) if pairs else 0.0

    return {
        "users": len(targets),
        "held_out": len(held),
        "cf_enabled": model is not None,
        f"recall@{k}": mean(results["hybrid"], 0),
        f"ndcg@{k}": mean(results["hybrid"], 1),
        f"popular_recall@{k}": mean(results["popular"], 0),
        f"popular_ndcg@{k}": mean(results["popular"], 1),
    }


def _metrics(got: list[int], wanted: set[int], k: int) -> tuple[float, float]:
    hits = [1.0 if a in wanted else 0.0 for a in got[:k]]
    recall = sum(hits) / min(len(wanted), k)
    dcg = sum(h / math.log2(i + 2) for i, h in enumerate(hits))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(wanted), k)))
    return recall, (dcg / ideal if ideal else 0.0)
