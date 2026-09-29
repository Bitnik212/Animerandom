from __future__ import annotations

import shutil
from io import StringIO

import pytest
from django.conf import settings
from django.core.management import call_command
from django.db import connections

from catalog.models import Anime
from pipeline.idmap import mapping
from pipeline.models import IdMapping, IdSourceEntry
from pipeline.sinks import elasticsearch as es_sink
from runs.contract import CONTRACT_PATH, dump_catalog_schema
from tests.conftest import DATABASES

pytestmark = pytest.mark.django_db(databases=DATABASES)


def test_no_missing_migrations():
    out = StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump not installed")
def test_catalog_contract_is_current():
    db = {**settings.DATABASES["catalog"], "NAME": connections["catalog"].settings_dict["NAME"]}
    fresh = dump_catalog_schema(db)
    assert CONTRACT_PATH.read_text(encoding="utf-8") == fresh, (
        "contract/catalog-schema.sql is stale: run `manage.py dump_catalog_contract` and commit it"
    )


def test_tables_live_in_their_schemas():
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_name IN "
            "('anime', 'raw_source_record', 'runs_ingestrun', 'pipeline_idmapping')"
        )
        found = set(cursor.fetchall())
    assert found == {
        ("catalog", "anime"),
        ("catalog", "raw_source_record"),
        ("ingest", "runs_ingestrun"),
        ("ingest", "pipeline_idmapping"),
    }


def _anime(anilist_id: int, **fields) -> Anime:
    return Anime.objects.create(anilist_id=anilist_id, **fields)


def test_es_rebuild_swaps_alias_and_drops_old_index(fake_es, settings):
    a = _anime(1, format="TV", status="FINISHED", episodes=12, season_year=1999)
    _anime(2, status="REMOVED")
    first = es_sink.rebuild()
    assert first == {"index": "anime_v1", "documents": 1, "replaced": []}
    assert fake_es.docs()[str(a.id)]["decade"] == "1990s"
    assert fake_es.mappings["anime_v1"]["dynamic"] == "strict"

    settings.ES_INDEX_VERSION = 2
    second = es_sink.rebuild()
    assert second["replaced"] == ["anime_v1"]
    assert fake_es.aliases == {"anime_v2": {"anime"}}

    third = es_sink.rebuild()  # same version again: fresh sibling, still atomic
    assert third["index"].startswith("anime_v2_") and third["replaced"] == ["anime_v2"]


def test_es_upsert_indexes_changes_and_deletes_removed(fake_es):
    a = _anime(1, status="FINISHED")
    es_sink.rebuild()
    b = _anime(2, status="RELEASING")
    Anime.objects.filter(id=a.id).update(status="REMOVED")
    assert es_sink.upsert([a.id, b.id]) == {"documents": 1, "deleted": 1}
    assert set(fake_es.docs()) == {str(b.id)}


def test_es_upsert_without_index_is_a_noop(fake_es):
    assert es_sink.upsert([1])["skipped_no_index"] == 1


def test_idmap_prefers_anilist_and_fills_gaps():
    IdSourceEntry.objects.bulk_create(
        [
            IdSourceEntry(source="arm", anilist_id=1, mal_id=1, annict_id=10),
            IdSourceEntry(source="arm", anilist_id=2, mal_id=999, annict_id=20),
            IdSourceEntry(source="manami", anilist_id=3, mal_id=33),
            IdSourceEntry(source="arm", anilist_id=None, mal_id=33, annict_id=30),
        ]
    )
    result = mapping.resolve({1: 1, 2: 22, 3: None, 4: None})
    mapping.save(result)
    by_id = IdMapping.objects.in_bulk([1, 2, 3, 4])
    assert (by_id[1].mal_id, by_id[1].annict_id) == (1, 10)
    assert (by_id[2].mal_id, by_id[2].mal_via) == (22, "anilist")  # AniList wins
    assert (by_id[3].mal_id, by_id[3].mal_via, by_id[3].annict_id) == (
        33,
        "manami",
        30,
    )  # arm via MAL id
    assert by_id[4].mal_id is None and by_id[4].annict_id is None
    assert result.conflicts == [(2, "MAL id: AniList says 22, arm says 999")]


@pytest.mark.django_db(databases=DATABASES, transaction=True)
def test_concurrent_batches_claiming_one_mal_id_do_not_fail():
    """Two merge shards writing anime that resolve to the same MAL id at the same time:
    one keeps it, the other is stored without it and reports a conflict."""
    import threading

    from django.db import connections as conns

    from pipeline.merge.precedence import MergeInput, merge
    from pipeline.sinks.postgres import write_batch
    from pipeline.sources.anilist import Media

    def merged(anilist_id: int):
        m = merge(
            MergeInput(Media(id=anilist_id, title={"romaji": f"Title {anilist_id}"}), mal_id=777)
        )
        return [(m, f"hash-{anilist_id}")]

    barrier = threading.Barrier(2)
    results, errors = [], []

    def shard(anilist_id: int) -> None:
        try:
            barrier.wait()
            results.append(write_batch(merged(anilist_id)))
        except Exception as exc:  # pragma: no cover - the failure being guarded against
            errors.append(exc)
        finally:
            conns.close_all()

    threads = [threading.Thread(target=shard, args=(i,)) for i in (501, 502)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert Anime.objects.filter(mal_id=777).count() == 1
    assert Anime.objects.filter(anilist_id__in=[501, 502]).count() == 2
    assert sum(len(r.conflicts) for r in results) == 1
