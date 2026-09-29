"""Random-pick pools in Redis (see infra/README.md#redis).

Each set is built in `{key}:next` and RENAMEd over the live key, so readers never
see a half-built set. Adult and REMOVED anime are never pooled.
"""

from __future__ import annotations

from collections import defaultdict

import redis

from catalog.models import Anime, AnimeGenre
from pipeline.derived import SCORE_POOLS, decade, length_bucket
from pipeline.http import redis_client
from pipeline.sinks.postgres import poolable

PREFIX = "pool:"
CHUNK = 5000


def compute() -> dict[str, set[int]]:
    pools: dict[str, set[int]] = defaultdict(set)
    rows = Anime.objects.filter(poolable()).values_list(
        "id", "format", "score", "season_year", "episodes"
    )
    ids: set[int] = set()
    for anime_id, fmt, score, season_year, episodes in rows.iterator(chunk_size=CHUNK):
        ids.add(anime_id)
        pools["pool:all"].add(anime_id)
        if fmt:
            pools[f"pool:format:{fmt}"].add(anime_id)
        for n in SCORE_POOLS:
            if score is not None and score >= n:
                pools[f"pool:score:{n}plus"].add(anime_id)
        if d := decade(season_year):
            pools[f"pool:decade:{d}"].add(anime_id)
        if bucket := length_bucket(episodes):
            pools[f"pool:length:{bucket}"].add(anime_id)
    for anime_id, slug in AnimeGenre.objects.values_list("anime_id", "genre__slug").iterator(
        chunk_size=CHUNK
    ):
        if anime_id in ids:
            pools[f"pool:genre:{slug}"].add(anime_id)
    return dict(pools)


def _live_keys(r: redis.Redis) -> set[str]:
    return {k for k in r.scan_iter(match=f"{PREFIX}*", count=1000) if not k.endswith(":next")}


def rebuild(r: redis.Redis | None = None) -> dict[str, int]:
    r = r or redis_client()
    pools = compute()
    for key, members in pools.items():
        staging = f"{key}:next"
        r.delete(staging)
        values = list(members)
        for i in range(0, len(values), CHUNK):
            r.sadd(staging, *values[i : i + CHUNK])
        r.rename(staging, key)
    stale = _live_keys(r) - set(pools)
    if stale:
        r.delete(*stale)
    return {"pools": len(pools), "removed": len(stale), "all": len(pools.get("pool:all", ()))}
