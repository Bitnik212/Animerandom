"""Stage logic as plain functions. Celery tasks in `tasks.py` are thin wrappers.

Fetch functions take a cursor and report progress through `on_progress`, so a task
that has to back off (rate limit, transient error) retries from where it stopped.
Stages find their work through `RunAnime` rows, never through id lists in the broker.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Mod
from django.utils import timezone
from pydantic import ValidationError

from catalog.models import Anime, AnimeOverride, RawSourceRecord
from pipeline import vocab
from pipeline.http import ItemFailed, SourceClient
from pipeline.idmap import arm, manami, mapping, vector
from pipeline.localize.fallback import localize
from pipeline.merge.precedence import MergedAnime, MergeInput, Override, merge
from pipeline.models import IdMapping, IdSourceEntry
from pipeline.raw import load_raw, payload_hash, store_raw
from pipeline.sinks import elasticsearch as es_sink
from pipeline.sinks import postgres as pg_sink
from pipeline.sinks import redis_pools
from pipeline.sources import anilist, annict, shikimori
from runs import services as runs
from runs.models import IngestRun, RunAnime, RunItemError, RunMode, RunStage

log = logging.getLogger(__name__)

# Bump when merge or localize logic changes so the next run rewrites every row.
MERGE_VERSION = "1"
# Payload keys that change without affecting any catalog value.
VOLATILE_KEYS = {"anilist": ("updatedAt",)}
CHUNK = 200

Progress = Callable[[Any], None]


def _noop(_: Any) -> None:
    pass


# --- Start ----------------------------------------------------------------------


def start(run_id: int) -> None:
    runs.mark_running(run_id)
    run = IngestRun.objects.get(id=run_id)
    vocab.sync()
    if not IdSourceEntry.objects.filter(source="manami").exists():
        manami.load()
    if (
        run.mode in (RunMode.FULL, RunMode.INCREMENTAL, RunMode.REFRESH)
        and not IdSourceEntry.objects.filter(source="arm").exists()
    ):
        try:
            arm.refresh()
        except Exception as exc:  # the run still works, Annict just stays unmapped
            runs.record_item(run_id, "map_ids", f"arm download failed: {exc!r}", source="arm")
    sources = run.params.get("sources") or ["anilist", "shikimori", "annict"]
    if run.mode == RunMode.MERGE or (
        run.mode in (RunMode.FULL, RunMode.INCREMENTAL) and "anilist" not in sources
    ):
        _seed_from_raw(run)


def _seed_from_raw(run: IngestRun) -> None:
    """Runs that don't fetch AniList work on the AniList records already stored."""
    ids = RawSourceRecord.objects.filter(source=anilist.SOURCE).values_list("source_id", flat=True)
    anilist_ids = sorted(int(i) for i in ids)
    if limit := run.params.get("limit"):
        top = Anime.objects.order_by("-popularity").values_list("anilist_id", flat=True)[:limit]
        anilist_ids = sorted(set(top) & set(anilist_ids)) or anilist_ids[:limit]
    RunAnime.objects.bulk_create(
        [RunAnime(run_id=run.id, anilist_id=i) for i in anilist_ids],
        ignore_conflicts=True,
        batch_size=2000,
    )


# --- Fetch: AniList ---------------------------------------------------------------


def _record_anilist(run_id: int, media: list[dict]) -> None:
    changed = store_raw(anilist.SOURCE, [(m["id"], m) for m in media])
    rows = [
        RunAnime(run_id=run_id, anilist_id=m["id"], anilist_changed=str(m["id"]) in changed)
        for m in media
    ]
    RunAnime.objects.bulk_create(rows, ignore_conflicts=True)
    if changed:
        RunAnime.objects.filter(run_id=run_id, anilist_id__in=[int(i) for i in changed]).update(
            anilist_changed=True
        )
    runs.stage_count(run_id, "fetch_anilist", processed=len(media), changed=len(changed))


def fetch_anilist_range(
    run_id: int, start: int, end: int | None, after_id: int | None, on_progress: Progress = _noop
) -> None:
    with anilist.client() as c:
        for media in anilist.iter_id_range(c, anilist.IdRange(start, end), after_id):
            _record_anilist(run_id, media)
            on_progress(max(m["id"] for m in media))


def fetch_anilist_popular(run_id: int, page: int, limit: int) -> None:
    per_page = settings.ANILIST_PER_PAGE
    with anilist.client() as c:
        media, _ = anilist.fetch_page(c, anilist.PAGE_BY_POPULARITY_QUERY, {"page": page})
    offset = (page - 1) * per_page
    _record_anilist(run_id, media[: max(0, limit - offset)])


def fetch_anilist_updated(run_id: int, page: int, on_progress: Progress = _noop) -> None:
    """Pages of recently updated anime, newest first, until older than INCREMENTAL_DAYS."""
    cutoff = (timezone.now() - timedelta(days=settings.INCREMENTAL_DAYS)).timestamp()
    with anilist.client() as c:
        while True:
            media, has_next = anilist.fetch_page(c, anilist.PAGE_BY_UPDATED_QUERY, {"page": page})
            fresh = [m for m in media if (m.get("updatedAt") or 0) >= cutoff]
            if fresh:
                _record_anilist(run_id, fresh)
            if not has_next or len(fresh) < len(media):
                return
            page += 1
            on_progress(page)


def fetch_anilist_airing(run_id: int, after_id: int | None, on_progress: Progress = _noop) -> None:
    with anilist.client() as c:
        for media in anilist.iter_id_range(
            c, anilist.IdRange(0, None), after_id, status="RELEASING"
        ):
            _record_anilist(run_id, media)
            on_progress(max(m["id"] for m in media))


def fetch_anilist_ids(run_id: int, ids: list[int], on_progress: Progress = _noop) -> None:
    """Refresh specific anime. `ids` shrinks as batches complete."""
    remaining = list(ids)
    with anilist.client() as c:
        while remaining:
            batch, rest = remaining[:50], remaining[50:]
            media = anilist.fetch_by_ids(c, batch)
            _record_anilist(run_id, media)
            for missing in set(batch) - {m["id"] for m in media}:
                runs.record_item(
                    run_id,
                    "fetch_anilist",
                    "not found on AniList",
                    source="anilist",
                    item_id=missing,
                )
            remaining = rest
            on_progress(remaining)


# --- Map ids --------------------------------------------------------------------------


def _run_anilist_ids(
    run_id: int, shard: int = 0, shards: int = 1, after: int | None = None
) -> Iterator[list[int]]:
    qs = RunAnime.objects.filter(run_id=run_id)
    if shards > 1:
        qs = qs.annotate(shard=Mod("anilist_id", shards)).filter(shard=shard)
    cursor = after or 0
    while True:
        chunk = list(
            qs.filter(anilist_id__gt=cursor)
            .order_by("anilist_id")
            .values_list("anilist_id", flat=True)[:CHUNK]
        )
        if not chunk:
            return
        yield chunk
        cursor = chunk[-1]


def map_ids(run_id: int) -> None:
    runs.stage_started(run_id, "map_ids")
    for chunk in _run_anilist_ids(run_id):
        raw = load_raw(anilist.SOURCE, chunk)
        anilist_mal = {
            i: (raw[str(i)].payload.get("idMal") if str(i) in raw else None) for i in chunk
        }
        result = mapping.resolve(anilist_mal)
        mapping.save(result)
        for anilist_id, message in result.conflicts:
            runs.record_item(
                run_id, "map_ids", message, item_id=anilist_id, kind="conflict", unique=True
            )
        runs.stage_count(
            run_id,
            "map_ids",
            processed=len(chunk),
            with_mal=sum(1 for m in result.mappings if m.mal_id),
            with_annict=sum(1 for m in result.mappings if m.annict_id),
        )


# --- Fetch: Shikimori and Annict ---------------------------------------------------------


def recheck_after(anilist_id: int) -> timedelta:
    """VECTOR_RECHECK_DAYS plus a stable per-anime offset of 0..SPREAD-1 days."""
    spread = max(1, settings.VECTOR_RECHECK_SPREAD_DAYS)
    return timedelta(days=settings.VECTOR_RECHECK_DAYS + anilist_id % spread)


def _search_due(anilist_id: int, searched_at: datetime | None, force: bool) -> bool:
    if force or searched_at is None:
        return True
    return searched_at < timezone.now() - recheck_after(anilist_id)


def _search_budget_left(run_id: int, stage: str, force: bool) -> bool:
    """False once this run has used VECTOR_SEARCHES_PER_RUN searches on this source
    (reported once); refresh runs are never capped."""
    if force:
        return True
    used = RunStage.objects.get(run_id=run_id, name=stage).counts.get("vector_searched", 0)
    if used < settings.VECTOR_SEARCHES_PER_RUN:
        return True
    runs.record_item(
        run_id,
        stage,
        f"vector search budget of {settings.VECTOR_SEARCHES_PER_RUN} reached; "
        "the remaining unmatched anime are searched on later runs",
        item_id="vector-search-budget",
        kind="skipped",
        unique=True,
    )
    return False


def _is_refresh(run_id: int) -> bool:
    return IngestRun.objects.filter(id=run_id, mode=RunMode.REFRESH).exists()


def _report_review(
    run_id: int, stage: str, anilist_id: int, source: str, text: str, result: vector.Match
) -> None:
    runs.record_item(
        run_id,
        stage,
        f"{source}: no confident match for {text!r}; candidates: {result.describe()}",
        source=source,
        item_id=anilist_id,
        kind="review",
        unique=True,
    )


def fetch_shikimori(
    run_id: int, shard: int, shards: int, after: int | None, on_progress: Progress = _noop
) -> None:
    """Fetch by MAL id (or an overridden Shikimori id). Anime without either get a
    title search matched by vector similarity."""
    force = _is_refresh(run_id)
    with shikimori.client() as c:
        for chunk in _run_anilist_ids(run_id, shard, shards, after):
            maps = IdMapping.objects.in_bulk(chunk)
            to_search = [
                i
                for i, m in maps.items()
                if not m.mal_id
                and m.shikimori_via != "override"
                and _search_due(i, m.shikimori_searched_at, force)
            ]
            media = load_raw(anilist.SOURCE, to_search) if to_search else {}
            for anilist_id in chunk:
                m = maps.get(anilist_id)
                key = (m.shikimori_id if m.shikimori_via == "override" else m.mal_id) if m else None
                try:
                    if m and key:
                        _shikimori_fetch(run_id, c, m, key)
                    elif (
                        m
                        and str(anilist_id) in media
                        and _search_budget_left(run_id, "fetch_shikimori", force)
                    ):
                        _shikimori_match(run_id, c, m, media[str(anilist_id)].payload)
                except ItemFailed as exc:
                    runs.record_item(
                        run_id,
                        "fetch_shikimori",
                        str(exc),
                        source="shikimori",
                        item_id=key or anilist_id,
                    )
                on_progress(anilist_id)


def _shikimori_fetch(run_id: int, c: SourceClient, m: IdMapping, key: int | str) -> None:
    payload = shikimori.fetch_by_id(c, key)
    if payload is None:
        m.shikimori_id = None
        runs.stage_count(run_id, "fetch_shikimori", processed=1, missing=1)
    else:
        changed = store_raw(shikimori.SOURCE, [(payload["id"], payload)])
        m.shikimori_id = str(payload["id"])
        runs.stage_count(run_id, "fetch_shikimori", processed=1, changed=len(changed))
    m.save(update_fields=["shikimori_id", "updated_at"])


def _shikimori_match(run_id: int, c: SourceClient, m: IdMapping, media: dict[str, Any]) -> None:
    text = vector.search_text(media)
    if text:
        results = shikimori.search(c, text, adult=bool(media.get("isAdult")))
        result = vector.match(
            vector.from_anilist(media), [vector.from_shikimori(r) for r in results]
        )
        if result.accepted and result.best:
            payload = shikimori.fetch_by_id(c, result.accepted.id)
            if payload is not None:
                store_raw(shikimori.SOURCE, [(payload["id"], payload)])
                m.shikimori_id = str(payload["id"])
                m.mal_id = payload.get("myanimelist_id") or None
                m.mal_via = "vector" if m.mal_id else ""
                m.mal_score = round(result.best.score, 3)
                runs.stage_count(run_id, "fetch_shikimori", processed=1, vector_matched=1)
        elif result.decision == "review":
            _report_review(run_id, "fetch_shikimori", m.anilist_id, "shikimori", text, result)
        runs.stage_count(run_id, "fetch_shikimori", vector_searched=1)
    m.shikimori_searched_at = timezone.now()
    m.save(
        update_fields=[
            "shikimori_id",
            "mal_id",
            "mal_via",
            "mal_score",
            "shikimori_searched_at",
            "updated_at",
        ]
    )


def fetch_annict(
    run_id: int, shard: int, shards: int, after: int | None, on_progress: Progress = _noop
) -> None:
    """Fetch by Annict id in batches. Anime without one get a title search matched by
    vector similarity (or by the work's own MAL id when it has one)."""
    if not annict.enabled():
        runs.record_item(
            run_id,
            "fetch_annict",
            "ANNICT_TOKEN is not set; Annict skipped",
            source="annict",
            kind="skipped",
            unique=True,
        )
        return
    force = _is_refresh(run_id)
    with annict.client() as c:
        for chunk in _run_anilist_ids(run_id, shard, shards, after):
            maps = IdMapping.objects.in_bulk(chunk)
            ids = sorted({m.annict_id for m in maps.values() if m.annict_id})
            for i in range(0, len(ids), annict.BATCH_SIZE):
                batch = ids[i : i + annict.BATCH_SIZE]
                try:
                    works = annict.fetch_works(c, batch)
                except ItemFailed as exc:
                    runs.record_item(
                        run_id,
                        "fetch_annict",
                        str(exc),
                        source="annict",
                        item_id=f"{batch[0]}..{batch[-1]}",
                    )
                    continue
                changed = store_raw(annict.SOURCE, [(w["annictId"], w) for w in works])
                runs.stage_count(
                    run_id,
                    "fetch_annict",
                    processed=len(works),
                    changed=len(changed),
                    missing=len(batch) - len(works),
                )
            to_search = [
                i
                for i, m in maps.items()
                if not m.annict_id and _search_due(i, m.annict_searched_at, force)
            ]
            media = load_raw(anilist.SOURCE, to_search) if to_search else {}
            for anilist_id in sorted(to_search):
                if str(anilist_id) not in media:
                    continue
                if not _search_budget_left(run_id, "fetch_annict", force):
                    break
                try:
                    _annict_match(run_id, c, maps[anilist_id], media[str(anilist_id)].payload)
                except ItemFailed as exc:
                    runs.record_item(
                        run_id, "fetch_annict", str(exc), source="annict", item_id=anilist_id
                    )
            on_progress(chunk[-1])


def _annict_match(run_id: int, c: SourceClient, m: IdMapping, media: dict[str, Any]) -> None:
    text = vector.search_text(media, prefer_native=True)
    if text:
        works = {str(w["annictId"]): w for w in annict.search_works(c, text)}
        chosen: tuple[dict, float] | None = None
        by_mal = [w for w in works.values() if m.mal_id and w.get("malAnimeId") == str(m.mal_id)]
        if len(by_mal) == 1:
            chosen = (by_mal[0], 1.0)  # Annict's own MAL link settles it
        else:
            result = vector.match(
                vector.from_anilist(media), [vector.from_annict(w) for w in works.values()]
            )
            if result.accepted and result.best:
                chosen = (works[result.accepted.id], result.best.score)
            elif result.decision == "review":
                _report_review(run_id, "fetch_annict", m.anilist_id, "annict", text, result)
        if chosen:
            work, score = chosen
            store_raw(annict.SOURCE, [(work["annictId"], work)])
            m.annict_id, m.annict_via, m.annict_score = work["annictId"], "vector", round(score, 3)
            runs.stage_count(run_id, "fetch_annict", processed=1, vector_matched=1)
        runs.stage_count(run_id, "fetch_annict", vector_searched=1)
    m.annict_searched_at = timezone.now()
    m.save(
        update_fields=[
            "annict_id",
            "annict_via",
            "annict_score",
            "annict_searched_at",
            "updated_at",
        ]
    )


# --- Merge, localize, write --------------------------------------------------------------


def _merge_hash(parts: list[Any]) -> str:
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def build_inputs(
    anilist_ids: list[int],
) -> dict[int, tuple[MergeInput, str, list[RawSourceRecord]]]:
    """Merge inputs for each AniList id with a stored payload, plus their merge hash."""
    raw_al = load_raw(anilist.SOURCE, anilist_ids)
    maps = IdMapping.objects.in_bulk(anilist_ids)
    raw_sh = load_raw(shikimori.SOURCE, [m.shikimori_id for m in maps.values() if m.shikimori_id])
    raw_an = load_raw(annict.SOURCE, [m.annict_id for m in maps.values() if m.annict_id])
    anime_ids = dict(
        Anime.objects.filter(anilist_id__in=anilist_ids).values_list("anilist_id", "id")
    )
    overrides: dict[int, list[Override]] = {}
    for o in AnimeOverride.objects.filter(anime_id__in=list(anime_ids.values())).order_by("id"):
        overrides.setdefault(o.anime_id, []).append(Override(o.field, o.locale, o.value))

    out = {}
    for anilist_id in anilist_ids:
        al = raw_al.get(str(anilist_id))
        if al is None:
            continue
        mp = maps.get(anilist_id)
        sh = raw_sh.get(mp.shikimori_id) if mp and mp.shikimori_id else None
        an = raw_an.get(str(mp.annict_id)) if mp and mp.annict_id else None
        ovr = overrides.get(anime_ids.get(anilist_id, -1), [])
        inp = MergeInput(
            anilist=anilist.Media.model_validate(al.payload),
            shikimori=shikimori.Anime.model_validate(sh.payload) if sh else None,
            annict=annict.Work.model_validate(an.payload) if an else None,
            mal_id=mp.mal_id if mp else al.payload.get("idMal"),
            shikimori_id=mp.shikimori_id if mp else None,
            annict_id=mp.annict_id if mp else None,
            overrides=ovr,
        )
        stable_al = {k: v for k, v in al.payload.items() if k not in VOLATILE_KEYS["anilist"]}
        digest = _merge_hash(
            [
                MERGE_VERSION,
                payload_hash(stable_al),
                sh.payload_hash if sh else None,
                an.payload_hash if an else None,
                inp.mal_id,
                inp.shikimori_id,
                inp.annict_id,
                [(o.field, o.locale, o.value) for o in ovr],
            ]
        )
        out[anilist_id] = (inp, digest, [r for r in (al, sh, an) if r])
    return out


def merge_one(inp: MergeInput) -> MergedAnime:
    return localize(merge(inp))


def merge_write(
    run_id: int,
    shard: int,
    shards: int,
    after: int | None,
    force: bool = False,
    on_progress: Progress = _noop,
) -> None:
    for chunk in _run_anilist_ids(run_id, shard, shards, after):
        try:
            inputs = build_inputs(chunk)
        except ValidationError:
            inputs = {}
            for anilist_id in chunk:  # find the bad payload, keep the rest of the chunk
                try:
                    inputs.update(build_inputs([anilist_id]))
                except ValidationError as exc:
                    runs.record_item(
                        run_id, "merge_write", f"invalid payload: {exc}", item_id=anilist_id
                    )
        current = dict(
            Anime.objects.filter(anilist_id__in=list(inputs)).values_list(
                "anilist_id", "merge_hash"
            )
        )
        batch: list[tuple[MergedAnime, str]] = []
        for anilist_id, (inp, digest, _) in inputs.items():
            if not force and current.get(anilist_id) == digest:
                continue
            merged = merge_one(inp)
            batch.append((merged, digest))
            for name in merged.unknown_genres:
                runs.record_item(
                    run_id,
                    "merge_write",
                    f"unknown AniList genre {name!r}",
                    item_id=f"genre:{name}",
                    kind="skipped",
                    unique=True,
                )
            for name in merged.unknown_tags:
                runs.record_item(
                    run_id,
                    "merge_write",
                    f"AniList tag {name!r} not in vocab/tags.yaml",
                    item_id=f"tag:{name}",
                    kind="skipped",
                    unique=True,
                )
        result = pg_sink.write_batch(batch)
        for anilist_id, message in result.conflicts:
            runs.record_item(
                run_id, "merge_write", message, item_id=anilist_id, kind="conflict", unique=True
            )
        if result.written:
            RunAnime.objects.filter(run_id=run_id, anilist_id__in=result.written).update(
                written=True
            )
        runs.stage_count(
            run_id,
            "merge_write",
            processed=len(chunk),
            written=len(result.written),
            unchanged=len(inputs) - len(batch),
            missing_payload=len(chunk) - len(inputs),
        )
        on_progress(chunk[-1])


def merge_preview(anilist_id: int) -> dict[str, Any] | None:
    inputs = build_inputs([anilist_id])
    if anilist_id not in inputs:
        return None
    merged = merge_one(inputs[anilist_id][0])
    return {
        "anilistId": anilist_id,
        "fields": {
            key: {
                "value": choice.value,
                "source": choice.source,
                "candidates": [{"source": s, "value": v} for s, v in choice.candidates],
            }
            for key, choice in merged.choices.items()
        },
        "genres": merged.genres,
        "tags": [t.slug for t in merged.tags],
        "unknownGenres": merged.unknown_genres,
        "unknownTags": merged.unknown_tags,
    }


# --- After merge ---------------------------------------------------------------------------


def link_relations(run_id: int) -> None:
    runs.stage_started(run_id, "link_relations")
    runs.stage_count(run_id, "link_relations", linked=pg_sink.link_relations())


def mark_removed(run_id: int) -> None:
    """Only after a complete, unlimited full run whose AniList fetch had no failures."""
    runs.stage_started(run_id, "mark_removed")
    run = IngestRun.objects.get(id=run_id)
    fetch_failures = RunItemError.objects.filter(
        run_id=run_id, stage="fetch_anilist", kind="error"
    ).exists()
    sources = run.params.get("sources") or ["anilist"]
    if run.params.get("limit") or fetch_failures or "anilist" not in sources:
        runs.stage_set(run_id, "mark_removed", skipped=1)
        return
    seen = set(RunAnime.objects.filter(run_id=run_id).values_list("anilist_id", flat=True))
    removed = pg_sink.mark_removed(seen)
    runs.stage_count(run_id, "mark_removed", removed=len(removed))


def index(run_id: int) -> None:
    runs.stage_started(run_id, "index")
    run = IngestRun.objects.get(id=run_id)
    if run.mode in (RunMode.FULL, RunMode.INDEX):
        runs.stage_set(run_id, "index", **_json_counts(es_sink.rebuild()))
        return
    written = RunAnime.objects.filter(run_id=run_id, written=True).values_list(
        "anilist_id", flat=True
    )
    anime_ids = list(
        Anime.objects.filter(Q(anilist_id__in=list(written))).values_list("id", flat=True)
    )
    runs.stage_set(run_id, "index", **es_sink.upsert(anime_ids))


def pools(run_id: int) -> None:
    runs.stage_started(run_id, "pools")
    runs.stage_set(run_id, "pools", **redis_pools.rebuild())


def _json_counts(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if isinstance(v, int | str | list)}
