"""ninja endpoints with signed test JWTs, including every rejection rule."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from ingest import auth
from pipeline import orchestration
from runs import services as runs
from runs.models import IngestRun, RunMode
from tests.conftest import DATABASES

pytestmark = pytest.mark.django_db(databases=DATABASES)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != "test-key":
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid!r}")
        return type("Key", (), {"key": KEY.public_key()})()


@pytest.fixture(autouse=True)
def jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "jwks_client", lambda: FakeJwks())


def token(
    settings: Any,
    *,
    key: Any = KEY,
    kid: str = "test-key",
    roles: tuple[str, ...] = ("user", "admin"),
    **overrides: Any,
) -> str:
    now = int(time.time())
    claims = {
        "iss": settings.KEYCLOAK_ISSUER,
        "aud": ["anime-picker-api", "account"],
        "azp": settings.KEYCLOAK_CLIENT_ID,
        "sub": "3f0c2c55-6f0e-4d4c-9a57-1f2f6b3f4e10",
        "typ": "Bearer",
        "exp": now + 300,
        "iat": now,
        "nbf": now,
        "realm_access": {"roles": list(roles)},
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def call(method: str, path: str, bearer: str | None = None, **kwargs: Any) -> Any:
    headers = {"HTTP_AUTHORIZATION": f"Bearer {bearer}"} if bearer else {}
    return getattr(Client(), method)(
        f"/api/v1{path}", content_type="application/json", **headers, **kwargs
    )


@pytest.fixture
def no_enqueue(monkeypatch: pytest.MonkeyPatch) -> list[IngestRun]:
    """Create runs (and take the lock) without running the canvas."""
    started: list[IngestRun] = []

    def start(mode: str, params: dict | None = None) -> IngestRun:
        run = runs.create_run(mode, params or {})
        started.append(run)
        return run

    monkeypatch.setattr(orchestration, "start", start)
    return started


def test_valid_admin_token_starts_a_run(settings, no_enqueue):
    response = call(
        "post",
        "/runs",
        token(settings),
        data={"mode": "full", "limit": 500, "sources": ["anilist", "shikimori"]},
    )
    assert response.status_code == 202
    run = no_enqueue[0]
    assert response.json() == {"runId": run.id}
    assert run.params["limit"] == 500 and run.params["sources"] == ["anilist", "shikimori"]


def test_second_run_is_409(settings, no_enqueue):
    call("post", "/runs", token(settings), data={"mode": "incremental"})
    response = call("post", "/runs", token(settings), data={"mode": "full"})
    assert response.status_code == 409
    assert response["Content-Type"] == "application/problem+json"
    assert response.json()["type"] == "run-in-progress"


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "http://evil.example/realms/anime-picker"},
        {"aud": "account"},
        {"azp": "some-other-client"},
        {"exp": int(time.time()) - 120},
        {"nbf": int(time.time()) + 120},
        {"typ": "ID"},
        {"typ": "Refresh"},
        {"sub": None},
    ],
    ids=[
        "wrong-iss",
        "wrong-aud",
        "wrong-azp",
        "expired",
        "not-yet-valid",
        "id-token",
        "refresh-token",
        "no-sub",
    ],
)
def test_rejections_are_401(settings, overrides):
    response = call("get", "/runs", token(settings, **overrides))
    assert response.status_code == 401
    assert response.json()["type"] == "unauthorized"
    assert response["WWW-Authenticate"] == "Bearer"


def test_expiry_within_leeway_is_accepted(settings):
    response = call("get", "/runs", token(settings, exp=int(time.time()) - 10))
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"kid": "unknown-kid"}, 401),
        ({"key": OTHER_KEY}, 401),
        ({"roles": ("user",)}, 403),
    ],
    ids=["unknown-kid", "bad-signature", "missing-admin-role"],
)
def test_key_and_role_checks(settings, kwargs, status):
    response = call("get", "/runs", token(settings, **kwargs))
    assert response.status_code == status
    assert response.json()["type"] == ("forbidden" if status == 403 else "unauthorized")


def test_missing_token_is_401():
    response = call("get", "/runs")
    assert response.status_code == 401
    assert response.json()["type"] == "unauthorized"


def test_run_detail_and_not_found(settings, no_enqueue):
    run_id = call("post", "/runs", token(settings), data={"mode": "full"}).json()["runId"]
    runs.record_item(run_id, "fetch_anilist", "HTTP 403", source="anilist", item_id="42")
    detail = call("get", f"/runs/{run_id}", token(settings)).json()
    assert [s["name"] for s in detail["stages"]] == list(runs.STAGES[RunMode.FULL])
    assert detail["itemErrors"][0]["itemId"] == "42"
    assert detail["stages"][0]["counts"] == {"failed": 1}

    missing = call("get", "/runs/999999", token(settings))
    assert missing.status_code == 404 and missing.json()["type"] == "run-not-found"


def test_cancel_releases_the_lock(settings, no_enqueue):
    run_id = call("post", "/runs", token(settings), data={"mode": "full"}).json()["runId"]
    response = call("post", f"/runs/{run_id}/cancel", token(settings))
    assert response.json()["status"] == "cancelled"
    assert runs.active_run_id() is None
    assert call("get", "/runs?status=cancelled", token(settings)).json()[0]["id"] == run_id


def test_merge_preview_not_found(settings):
    response = call("get", "/anime/123/merge-preview", token(settings))
    assert response.status_code == 404 and response.json()["type"] == "anime-not-found"


def test_sources_report(settings):
    body = call("get", "/sources", token(settings)).json()
    assert [s["source"] for s in body] == ["anilist", "shikimori", "annict"]
    assert body[2]["enabled"] is False  # no ANNICT_TOKEN in tests


def test_vocab_sync(settings):
    assert call("post", "/vocab/sync", token(settings)).json()["genres"] == 19


def test_healthz_needs_no_token():
    response = call("get", "/healthz")
    assert response.json()["checks"]["postgres.catalog"] == "ok"
    assert response.json()["checks"]["redis"] == "ok"
