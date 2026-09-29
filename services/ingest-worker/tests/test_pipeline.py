"""The whole pipeline through the Celery canvas (eager), against Postgres and Redis,
with sources mocked by respx and Elasticsearch faked."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
import respx

from catalog.models import (
    Anime,
    AnimeLocalization,
    AnimeRelation,
    AnimeSynonym,
    AnimeTag,
    RawSourceRecord,
)
from pipeline import orchestration, stages
from pipeline.models import IdMapping
from runs import services as runs
from runs.models import IngestRun, RunItemError, RunMode, RunStatus
from tests.conftest import DATABASES, fixture

pytestmark = pytest.mark.django_db(databases=DATABASES)

MEDIA = fixture("anilist_page.json")["data"]["Page"]["media"]
ARM = [
    {"mal_id": 9253, "anilist_id": 9253, "annict_id": 1745},
    {"mal_id": 12345, "anilist_id": 20958, "annict_id": 5000},  # disagrees with AniList's idMal
    {"mal_id": None, "anilist_id": 100001, "annict_id": 7000},
]


def page(media: list[dict], has_next: bool = False) -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"Page": {"pageInfo": {"hasNextPage": has_next}, "media": media}}}
    )


class Sources:
    """respx side effects standing in for AniList, Shikimori, Annict, and arm."""

    def __init__(self, media: list[dict]) -> None:
        self.media = media
        self.shikimori: dict[int, Any] = {
            16498: fixture("shikimori_16498.json"),
            9253: fixture("shikimori_9253.json"),
        }
        self.anilist_calls = 0
        self.anilist_hook: Any = None  # (call number) -> response or None

    def anilist(self, request: httpx.Request) -> httpx.Response:
        self.anilist_calls += 1
        if self.anilist_hook and (hooked := self.anilist_hook(self.anilist_calls)) is not None:
            return hooked
        body = json.loads(request.content)
        query, v = body["query"], body["variables"]
        if "id_in" in query:
            return page([m for m in self.media if m["id"] in v["ids"]])
        if "POPULARITY_DESC" in query:
            ranked = sorted(self.media, key=lambda m: -m["popularity"])
            per = v["perPage"]
            return page(ranked[(v["page"] - 1) * per : v["page"] * per])
        if "UPDATED_AT_DESC" in query:
            return page(sorted(self.media, key=lambda m: -m["updatedAt"]) if v["page"] == 1 else [])
        selected = [
            m
            for m in self.media
            if m["id"] > v["idGreater"]
            and m["id"] < v.get("idLesser", 10**9)
            and (v.get("status") is None or m["status"] == v["status"])
        ]
        selected.sort(key=lambda m: m["id"])
        return page(selected[: v["perPage"]], has_next=len(selected) > v["perPage"])

    def shiki(self, request: httpx.Request, mal_id: str) -> httpx.Response:
        payload = self.shikimori.get(int(mal_id))
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload) if payload else httpx.Response(404)

    def install(self, router: respx.MockRouter) -> None:
        router.post("https://graphql.anilist.co").mock(side_effect=self.anilist)
        router.get(url__regex=r"https://shikimori\.io/api/animes/(?P<mal_id>\w+)").mock(
            side_effect=self.shiki
        )
        router.post("https://api.annict.com/graphql").mock(
            return_value=httpx.Response(200, json=fixture("annict_works.json"))
        )
        router.get(url__regex=r"https://raw\.githubusercontent\.com/.*arm\.json").mock(
            return_value=httpx.Response(200, json=ARM)
        )


@pytest.fixture
def sources(annict_token: None) -> Any:
    src = Sources([dict(m) for m in MEDIA])
    with respx.mock(assert_all_called=False) as router:
        src.install(router)
        yield src


def run_pipeline(mode: str, **params: Any) -> IngestRun:
    run = orchestration.start(mode, params)
    run.refresh_from_db()
    return run


def titles(anilist_id: int) -> dict[str, AnimeLocalization]:
    return {
        loc.locale: loc for loc in AnimeLocalization.objects.filter(anime__anilist_id=anilist_id)
    }


def test_full_run_with_limit(eager_celery, sources, fake_es, isolated_redis):
    run = run_pipeline(RunMode.FULL, limit=6)

    assert run.status == RunStatus.DONE, run.error
    stage_list = list(run.stages.values_list("name", "status"))
    assert [name for name, _ in stage_list] == list(runs.STAGES[RunMode.FULL])
    assert all(status == RunStatus.DONE for _, status in stage_list)
    assert runs.active_run_id() is None

    # Raw payloads are stored before anything else.
    assert RawSourceRecord.objects.filter(source="anilist").count() == 6
    assert RawSourceRecord.objects.filter(source="shikimori").count() == 2
    assert RawSourceRecord.objects.filter(source="annict").count() == 1

    # Merged rows.
    assert Anime.objects.count() == 6
    aot = Anime.objects.get(anilist_id=16498)
    assert (float(aot.score), aot.mal_id, aot.shikimori_id, aot.episodes) == (
        8.5,
        16498,
        "16498",
        25,
    )
    t = titles(16498)
    assert t["en"].title == "Attack on Titan"
    assert (t["ru"].title, t["ru"].title_source) == ("Атака на титанов", "shikimori")
    assert t["ru"].synopsis.startswith("Много лет назад")
    assert t["ja"].title == "進撃の巨人"
    assert t["ja_latn"].title == "Shingeki no Kyojin" and not t["ja_latn"].title_machine
    generated = titles(100001)["ja_latn"]
    assert generated.title_machine and generated.title_source == "cutlet"
    assert AnimeSynonym.objects.filter(anime=aot, value="Вторжение гигантов", locale="ru").exists()
    assert AnimeTag.objects.filter(anime=aot, tag__slug="tragedy", is_spoiler=True).exists()

    # Relations in both directions, manga relation ignored.
    s2 = Anime.objects.get(anilist_id=20958)
    assert set(AnimeRelation.objects.values_list("anime_id", "related_id", "kind")) == {
        (aot.id, s2.id, "SEQUEL"),
        (s2.id, aot.id, "PREQUEL"),
    }

    # Id mapping: arm gives Annict ids; AniList's idMal wins a disagreement.
    assert IdMapping.objects.get(anilist_id=9253).annict_id == 1745
    assert s2.mal_id == 25777
    errors = RunItemError.objects.filter(run=run)
    assert errors.filter(kind="conflict", item_id="20958").exists()
    assert errors.filter(kind="skipped", item_id="tag:Kaiju").exists()
    assert errors.filter(kind="skipped", item_id="genre:Some New Genre").exists()
    assert not errors.filter(kind="error").exists()

    # Elasticsearch: one index behind the alias, one document per anime.
    assert fake_es.aliases == {"anime_v1": {"anime"}}
    docs = fake_es.docs()
    assert len(docs) == 6
    doc = docs[str(aot.id)]
    assert doc["title"]["ru"] == "Атака на титанов"
    assert doc["genres"] == ["action", "drama", "fantasy", "mystery"]
    assert "tragedy" not in doc["tags"]  # spoilers are not indexed
    assert (doc["decade"], doc["length"], doc["studios"]) == ("2010s", "medium", ["Wit Studio"])

    # Pools: adult titles never pooled.
    r = isolated_redis
    adult = Anime.objects.get(anilist_id=100002).id
    assert r.scard("pool:all") == 5 and not r.sismember("pool:all", adult)
    assert r.sismember("pool:genre:action", aot.id)
    assert r.smembers("pool:format:MOVIE") == {str(Anime.objects.get(anilist_id=21827).id)}
    assert r.sismember("pool:score:8plus", aot.id) and not r.sismember("pool:score:9plus", aot.id)
    assert r.sismember("pool:decade:2010s", aot.id)
    assert r.sismember("pool:length:long", Anime.objects.get(anilist_id=100001).id)
    assert not list(r.scan_iter("pool:*:next"))


def test_second_run_skips_unchanged(eager_celery, sources, fake_es):
    run_pipeline(RunMode.FULL, limit=6)
    now = int(time.time())
    for m in sources.media:
        m["updatedAt"] = now
    run = run_pipeline(RunMode.INCREMENTAL)
    assert run.status == RunStatus.DONE, run.error
    merge = run.stages.get(name="merge_write").counts
    assert merge["written"] == 0 and merge["unchanged"] == 6

    sources.media[0]["averageScore"] = 91
    sources.media[0]["updatedAt"] = now
    run = run_pipeline(RunMode.INCREMENTAL)
    assert run.stages.get(name="merge_write").counts["written"] == 1
    assert float(Anime.objects.get(anilist_id=16498).score) == 9.1
    assert fake_es.docs()[str(Anime.objects.get(anilist_id=16498).id)]["score"] == 9.1


def test_unlimited_full_run_marks_missing_anime_removed(
    eager_celery, sources, fake_es, settings, isolated_redis
):
    settings.ANILIST_ID_RANGE = 200_000
    run_pipeline(RunMode.FULL)
    sources.media = [m for m in sources.media if m["id"] != 9253]
    run = run_pipeline(RunMode.FULL)
    assert run.status == RunStatus.DONE, run.error
    sg = Anime.objects.get(anilist_id=9253)
    assert sg.status == "REMOVED"
    assert run.stages.get(name="mark_removed").counts == {"removed": 1}
    assert str(sg.id) not in fake_es.docs()
    assert not isolated_redis.sismember("pool:all", sg.id)


def test_limited_full_run_never_marks_removed(eager_celery, sources, fake_es):
    run_pipeline(RunMode.FULL, limit=6)
    sources.media = sources.media[:2]
    run = run_pipeline(RunMode.FULL, limit=6)
    assert run.stages.get(name="mark_removed").counts == {"skipped": 1}
    assert not Anime.objects.filter(status="REMOVED").exists()


def test_override_beats_sources_on_refresh(eager_celery, sources, fake_es):
    run_pipeline(RunMode.FULL, limit=6)
    aot = Anime.objects.get(anilist_id=16498)
    aot.overrides.create(field="title", locale="en", value="Attack on Titan (override)")
    run = run_pipeline(RunMode.REFRESH, anilist_ids=[16498])
    assert run.status == RunStatus.DONE, run.error
    assert titles(16498)["en"].title == "Attack on Titan (override)"
    assert titles(16498)["en"].title_source == "override"


def test_merge_mode_needs_no_network(eager_celery, sources, fake_es):
    run_pipeline(RunMode.FULL, limit=6)
    with respx.mock(assert_all_mocked=True):  # any request would fail the test
        run = run_pipeline(RunMode.MERGE, force=True)
    assert run.status == RunStatus.DONE, run.error
    assert run.stages.get(name="merge_write").counts["written"] == 6


def test_overlapping_runs_are_refused(eager_celery):
    runs.create_run(RunMode.POOLS)
    with pytest.raises(runs.RunInProgress):
        runs.create_run(RunMode.FULL)


def test_cancelled_run_stops(eager_celery, sources, fake_es):
    run = runs.create_run(RunMode.FULL, {"limit": 6})
    runs.cancel(run.id)
    orchestration.build(run).apply_async()
    run.refresh_from_db()
    assert run.status == RunStatus.CANCELLED
    assert not Anime.objects.exists()
    assert runs.active_run_id() is None


def test_unexpected_error_fails_the_run(eager_celery, sources, fake_es, monkeypatch):
    def boom(inp):
        raise RuntimeError("merge bug")

    monkeypatch.setattr(stages, "merge_one", boom)
    with pytest.raises(RuntimeError):
        orchestration.start(RunMode.FULL, {"limit": 6})
    run = IngestRun.objects.get()
    assert run.status == RunStatus.FAILED
    assert "merge bug" in run.error
    assert run.stages.get(name="merge_write").status == RunStatus.FAILED
    assert run.stages.get(name="fetch_anilist").status == RunStatus.DONE
    assert runs.active_run_id() is None


def test_source_4xx_fails_the_item_not_the_run(eager_celery, sources, fake_es):
    sources.shikimori[9253] = httpx.Response(403)
    run = run_pipeline(RunMode.FULL, limit=6)
    assert run.status == RunStatus.DONE, run.error
    assert RunItemError.objects.filter(
        run=run, stage="fetch_shikimori", kind="error", item_id="9253"
    ).exists()
    assert Anime.objects.filter(anilist_id=9253).exists()  # merged without Shikimori


def test_rate_limit_retries_resume_from_cursor(eager_celery, sources, fake_es, settings):
    settings.ANILIST_ID_RANGE = 200_000
    settings.ANILIST_PER_PAGE = 2
    sources.anilist_hook = lambda n: (
        httpx.Response(429, headers={"Retry-After": "1"}) if n == 2 else None
    )
    run = run_pipeline(RunMode.FULL)
    assert run.status == RunStatus.DONE, run.error
    assert Anime.objects.count() == 6
    assert run.stages.get(name="fetch_anilist").counts["processed"] == 6  # nothing fetched twice


def test_transient_errors_give_up_after_six_attempts(
    eager_celery, sources, fake_es, settings, monkeypatch
):
    from pipeline import tasks

    monkeypatch.setattr(tasks, "backoff", lambda attempt: 0)
    settings.ANILIST_ID_RANGE = 200_000
    sources.anilist_hook = lambda n: httpx.Response(503) if n <= 6 else None
    run = run_pipeline(RunMode.FULL)
    assert run.status == RunStatus.DONE, run.error
    failed = RunItemError.objects.get(run=run, stage="fetch_anilist", kind="error")
    assert "gave up after 6 attempts" in failed.message
    # The first range was lost, so this run must not mark anything REMOVED.
    assert run.stages.get(name="mark_removed").counts == {"skipped": 1}
