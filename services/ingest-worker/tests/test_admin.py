import pytest
from django.contrib.auth.models import User
from django.test import Client

from catalog.models import Anime, AnimeLocalization, AnimeOverride, RawSourceRecord
from pipeline import orchestration
from runs import services as runs
from runs.models import IngestRun, RunMode
from tests.conftest import DATABASES

pytestmark = pytest.mark.django_db(databases=DATABASES)


@pytest.fixture
def admin_client() -> Client:
    user = User.objects.create_superuser("admin", "admin@example.com", "pw")
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def anime() -> Anime:
    a = Anime.objects.create(anilist_id=16498, format="TV", popularity=10)
    AnimeLocalization.objects.create(
        anime=a, locale="en", title="Attack on Titan", title_source="anilist"
    )
    AnimeLocalization.objects.create(
        anime=a, locale="ja_latn", title="Shingeki no Kyojin", title_machine=True
    )
    RawSourceRecord.objects.create(
        source="anilist",
        source_id="16498",
        payload={"id": 16498},
        payload_hash="x",
        fetched_at="2026-09-29T00:00:00Z",
    )
    return a


def test_anime_list_search_and_detail(admin_client, anime):
    listing = admin_client.get("/admin/catalog/anime/", {"q": "Attack"})
    assert listing.status_code == 200 and "Attack on Titan" in listing.text
    detail = admin_client.get(f"/admin/catalog/anime/{anime.id}/change/")
    assert detail.status_code == 200
    assert "Shingeki no Kyojin" in detail.text and "machine" in detail.text
    assert "&quot;id&quot;: 16498" in detail.text or '"id": 16498' in detail.text


def test_saving_an_override_enqueues_a_refresh(admin_client, anime, monkeypatch):
    started = []

    def start(mode, params=None):
        run = runs.create_run(mode, params or {})
        started.append(run)
        return run

    monkeypatch.setattr(orchestration, "start", start)
    response = admin_client.post(
        "/admin/catalog/animeoverride/add/",
        {"anime": anime.id, "field": "title", "locale": "en", "value": "AoT", "note": "typo"},
    )
    assert response.status_code == 302
    override = AnimeOverride.objects.get()
    assert override.updated_by == "admin"
    assert started[0].mode == RunMode.REFRESH and started[0].params["anilist_ids"] == [16498]


def test_run_pages(admin_client):
    run = runs.create_run(RunMode.FULL, {"limit": 5})
    runs.record_item(run.id, "fetch_anilist", "HTTP 403", item_id="1")
    assert admin_client.get("/admin/runs/ingestrun/").status_code == 200
    page = admin_client.get(f"/admin/runs/ingestrun/{run.id}/change/")
    assert page.status_code == 200 and "fetch_anilist" in page.text
    assert IngestRun.objects.count() == 1


def test_vocab_pages(admin_client):
    from pipeline import vocab

    vocab.sync()
    page = admin_client.get("/admin/catalog/genre/")
    assert page.status_code == 200 and "Повседневность" in page.text


def test_view_only_staff_cannot_change_overrides(anime, monkeypatch):
    from django.contrib.auth.models import Permission

    started = []
    monkeypatch.setattr(orchestration, "start", lambda *a, **k: started.append(a))
    viewer = User.objects.create_user("viewer", password="pw", is_staff=True)
    viewer.user_permissions.add(Permission.objects.get(codename="view_anime"))
    client = Client()
    client.force_login(viewer)

    assert client.get(f"/admin/catalog/anime/{anime.id}/change/").status_code == 200
    response = client.post(
        f"/admin/catalog/anime/{anime.id}/change/",
        {
            "overrides-TOTAL_FORMS": "1",
            "overrides-INITIAL_FORMS": "0",
            "overrides-0-field": "title",
            "overrides-0-locale": "en",
            "overrides-0-value": "hijacked",
        },
    )
    assert response.status_code == 403
    assert not AnimeOverride.objects.exists() and started == []
