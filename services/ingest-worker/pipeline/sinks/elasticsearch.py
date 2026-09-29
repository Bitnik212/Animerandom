"""Elasticsearch: documents built from Postgres, full rebuild with atomic alias swap,
incremental bulk upserts."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from functools import cache
from typing import Any

from django.conf import settings
from elasticsearch import Elasticsearch

from catalog.models import (
    Anime,
    AnimeGenre,
    AnimeLocalization,
    AnimeStatus,
    AnimeStudio,
    AnimeSynonym,
    AnimeTag,
)
from pipeline.derived import decade, length_bucket

CHUNK = 500


@cache
def client() -> Elasticsearch:
    return Elasticsearch(settings.ELASTICSEARCH_URL, request_timeout=60)


def index_name(version: int | None = None) -> str:
    return f"{settings.ES_ALIAS}_v{version or settings.ES_INDEX_VERSION}"


def _chunks(ids: list[int], size: int) -> Iterator[list[int]]:
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def documents(anime_ids: Iterable[int]) -> Iterator[dict[str, Any]]:
    """One document per anime, shaped as in infra/README.md. REMOVED anime yield nothing."""
    for chunk in _chunks(sorted(anime_ids), CHUNK):
        anime = Anime.objects.filter(id__in=chunk).exclude(status=AnimeStatus.REMOVED)
        locs: dict[int, dict[str, AnimeLocalization]] = defaultdict(dict)
        for loc in AnimeLocalization.objects.filter(anime_id__in=chunk):
            locs[loc.anime_id][loc.locale] = loc
        synonyms: dict[int, list[str]] = defaultdict(list)
        for anime_id, value in AnimeSynonym.objects.filter(anime_id__in=chunk).values_list(
            "anime_id", "value"
        ):
            synonyms[anime_id].append(value)
        genres: dict[int, list[str]] = defaultdict(list)
        for anime_id, slug in AnimeGenre.objects.filter(anime_id__in=chunk).values_list(
            "anime_id", "genre__slug"
        ):
            genres[anime_id].append(slug)
        tags: dict[int, list[str]] = defaultdict(list)
        for anime_id, slug in (
            AnimeTag.objects.filter(anime_id__in=chunk, is_spoiler=False, tag__is_spoiler=False)
            .order_by("anime_id", "-rank")
            .values_list("anime_id", "tag__slug")
        ):
            tags[anime_id].append(slug)
        studios: dict[int, list[str]] = defaultdict(list)
        for anime_id, name in AnimeStudio.objects.filter(
            anime_id__in=chunk, is_main=True
        ).values_list("anime_id", "studio__name"):
            studios[anime_id].append(name)

        for a in anime:
            by_locale = locs.get(a.id, {})
            title = {
                k: (by_locale[k].title if k in by_locale else None)
                for k in ("en", "ru", "ja", "ja_latn")
            }
            synopsis = {
                k: (by_locale[k].synopsis if k in by_locale else None) for k in ("en", "ru", "ja")
            }
            suggest = [t for t in title.values() if t] + synonyms.get(a.id, [])
            yield {
                "id": a.id,
                "title": title,
                "synonyms": synonyms.get(a.id, []),
                "synopsis": synopsis,
                "title_suggest": list(dict.fromkeys(suggest)),
                "genres": genres.get(a.id, []),
                "tags": tags.get(a.id, []),
                "studios": studios.get(a.id, []),
                "format": a.format,
                "status": a.status,
                "season_year": a.season_year,
                "decade": decade(a.season_year),
                "length": length_bucket(a.episodes),
                "episodes": a.episodes,
                "score": float(a.score) if a.score is not None else None,
                "popularity": a.popularity,
                "is_adult": a.is_adult,
            }


class BulkError(Exception):
    pass


def bulk_index(es: Elasticsearch, index: str, docs: Iterable[dict[str, Any]]) -> int:
    count = 0
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        if not batch:
            return
        response = es.bulk(operations=batch)
        if response.get("errors"):
            failed = [i for i in response["items"] if "error" in next(iter(i.values()))]
            raise BulkError(f"{len(failed)} bulk errors, first: {json.dumps(failed[0])[:500]}")
        batch.clear()

    for doc in docs:
        batch += [{"index": {"_index": index, "_id": str(doc["id"])}}, doc]
        count += 1
        if len(batch) >= CHUNK * 2:
            flush()
    flush()
    return count


def rebuild(es: Elasticsearch | None = None) -> dict[str, Any]:
    """Create anime_v{N}, load every anime, then atomically point the alias at it and
    delete whatever index the alias pointed to before."""
    es = es or client()
    alias = settings.ES_ALIAS
    target = index_name()
    definition = json.loads(settings.ES_INDEX_DEFINITION.read_text(encoding="utf-8"))

    previous = (
        set(es.indices.get_alias(name=alias).keys())
        if es.indices.exists_alias(name=alias)
        else set()
    )
    if target in previous:
        # Same version rebuilt: load into a fresh sibling so the alias swap stays atomic.
        target = f"{target}_{int(time.time())}"
    if es.indices.exists(index=target):
        es.indices.delete(index=target)
    es.indices.create(
        index=target, settings=definition["settings"], mappings=definition["mappings"]
    )

    ids = list(Anime.objects.values_list("id", flat=True))
    count = bulk_index(es, target, documents(ids))
    es.indices.refresh(index=target)

    actions: list[dict[str, Any]] = [{"remove": {"index": old, "alias": alias}} for old in previous]
    actions.append({"add": {"index": target, "alias": alias}})
    es.indices.update_aliases(actions=actions)
    for old in previous:
        es.indices.delete(index=old)
    return {"index": target, "documents": count, "replaced": sorted(previous)}


def upsert(anime_ids: list[int], es: Elasticsearch | None = None) -> dict[str, int]:
    """Incremental: index changed anime into the index behind the alias; REMOVED ones
    are deleted from it."""
    es = es or client()
    alias = settings.ES_ALIAS
    if not es.indices.exists_alias(name=alias):
        return {"documents": 0, "deleted": 0, "skipped_no_index": 1}
    count = bulk_index(es, alias, documents(anime_ids))
    removed = list(
        Anime.objects.filter(id__in=anime_ids, status=AnimeStatus.REMOVED).values_list(
            "id", flat=True
        )
    )
    if removed:
        es.bulk(
            operations=[{"delete": {"_index": alias, "_id": str(i)}} for i in removed],
        )
    return {"documents": count, "deleted": len(removed)}
