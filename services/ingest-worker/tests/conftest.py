"""Test setup.

Tests need Postgres and Redis (the compose `ingest-test` service provides both, or
point POSTGRES_URL / REDIS_URL at your own). The Postgres role must be allowed to
create the test database. Elasticsearch is replaced by `FakeElasticsearch` unless a
test opts into a real one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connections

from pipeline import http as pipeline_http
from pipeline.sinks import elasticsearch as es_sink

FIXTURES = Path(__file__).parent / "fixtures"
DATABASES = ["default", "catalog"]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: None, django_db_blocker: Any) -> Any:
    # Both aliases share one test database, so Django migrates only `default`;
    # the catalog app migrates through its own alias.
    with django_db_blocker.unblock():
        call_command("migrate", database="catalog", verbosity=0)
    yield
    # Close the second alias's connection so the test database can be dropped.
    connections.close_all()


@pytest.fixture(autouse=True)
def isolated_redis(settings: Any) -> Any:
    base = settings.REDIS_URL.rsplit("/", 1)[0]
    settings.REDIS_URL = f"{base}/15"
    pipeline_http.redis_client.cache_clear()
    client = pipeline_http.redis_client()
    client.flushdb()
    yield client
    client.flushdb()
    pipeline_http.redis_client.cache_clear()


@pytest.fixture(autouse=True)
def fast_rates(settings: Any) -> None:
    settings.SOURCE_RATES_PER_MIN = {"anilist": 60_000, "shikimori": 60_000, "annict": 60_000}


@pytest.fixture
def eager_celery() -> Any:
    from ingest.celery import app

    # Keys carry the CELERY_ namespace prefix, which wins over unprefixed names.
    # Propagation stays off: with it on, eager tasks skip on_failure and retries.
    # A failed task still raises from the chain's result, like a real worker's would.
    app.conf.update(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=False)
    yield app
    app.conf.update(CELERY_TASK_ALWAYS_EAGER=False)


class _Indices:
    def __init__(self, es: FakeElasticsearch) -> None:
        self.es = es

    def exists_alias(self, name: str) -> bool:
        return any(name in a for a in self.es.aliases.values())

    def get_alias(self, name: str) -> dict[str, Any]:
        return {i: {"aliases": {name: {}}} for i, a in self.es.aliases.items() if name in a}

    def exists(self, index: str) -> bool:
        return index in self.es.indices_

    def delete(self, index: str) -> None:
        self.es.indices_.pop(index)
        self.es.aliases.pop(index, None)

    def create(self, index: str, settings: dict, mappings: dict) -> None:
        assert index not in self.es.indices_
        self.es.indices_[index] = {}
        self.es.aliases[index] = set()
        self.es.mappings[index] = mappings

    def refresh(self, index: str) -> None:
        pass

    def update_aliases(self, actions: list[dict]) -> None:
        for action in actions:
            ((op, spec),) = action.items()
            if op == "add":
                self.es.aliases[spec["index"]].add(spec["alias"])
            else:
                self.es.aliases[spec["index"]].discard(spec["alias"])


class FakeElasticsearch:
    """Just enough of the client for the sink: indices, aliases, and bulk."""

    def __init__(self) -> None:
        self.indices_: dict[str, dict[str, dict]] = {}
        self.aliases: dict[str, set[str]] = {}
        self.mappings: dict[str, dict] = {}
        self.indices = _Indices(self)

    def resolve(self, name: str) -> str:
        if name in self.indices_:
            return name
        (target,) = [i for i, a in self.aliases.items() if name in a]
        return target

    def bulk(self, operations: list[dict]) -> dict[str, Any]:
        items = []
        it = iter(operations)
        for action in it:
            ((op, meta),) = action.items()
            index = self.resolve(meta["_index"])
            if op == "index":
                self.indices_[index][meta["_id"]] = next(it)
            elif op == "delete":
                self.indices_[index].pop(meta["_id"], None)
            items.append({op: {"_id": meta["_id"], "status": 200}})
        return {"errors": False, "items": items}

    def docs(self, alias: str = "anime") -> dict[str, dict]:
        return self.indices_[self.resolve(alias)]


@pytest.fixture
def fake_es(monkeypatch: pytest.MonkeyPatch) -> FakeElasticsearch:
    es = FakeElasticsearch()
    monkeypatch.setattr(es_sink, "client", lambda: es)
    return es


@pytest.fixture
def annict_token(settings: Any) -> None:
    settings.ANNICT_TOKEN = "test-token"
