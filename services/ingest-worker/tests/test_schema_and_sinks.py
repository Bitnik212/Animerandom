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
