import pytest

from runs import services as runs
from runs.models import RunMode
from tests.conftest import DATABASES

pytestmark = pytest.mark.django_db(databases=DATABASES)


def test_lock_refresh_extends_only_the_owning_run(isolated_redis):
    run = runs.create_run(RunMode.POOLS)
    isolated_redis.expire(runs.LOCK_KEY, 60)
    assert runs.refresh_lock(run.id) is True
    assert isolated_redis.ttl(runs.LOCK_KEY) > runs.LOCK_TTL - 5

    isolated_redis.expire(runs.LOCK_KEY, 60)
    assert runs.refresh_lock(run.id + 1) is False  # a stale task of another run
    assert isolated_redis.ttl(runs.LOCK_KEY) <= 60

    runs.finish(run.id)
    assert runs.refresh_lock(run.id) is False  # a finished run never re-takes it
    assert runs.active_run_id() is None


def test_every_task_start_refreshes_the_lock(isolated_redis, eager_celery):
    from pipeline import tasks

    run = runs.create_run(RunMode.POOLS)
    isolated_redis.expire(runs.LOCK_KEY, 60)
    tasks._guard(run.id, None)
    assert isolated_redis.ttl(runs.LOCK_KEY) > 60
