"""Keycloak access-token validation for the ninja API, same rules as the main API."""

from __future__ import annotations

from functools import cache
from typing import Any

import jwt
from django.conf import settings
from django.http import HttpRequest
from ninja.security import HttpBearer

ADMIN_ROLE = "admin"


class Problem(Exception):
    """Rendered as RFC 7807 application/problem+json by the API's exception handler."""

    def __init__(self, type_: str, title: str, status: int, detail: str = "") -> None:
        super().__init__(detail or title)
        self.type = type_
        self.title = title
        self.status = status
        self.detail = detail


class Unauthorized(Problem):
    def __init__(self, detail: str) -> None:
        super().__init__("unauthorized", "Unauthorized", 401, detail)


class Forbidden(Problem):
    def __init__(self, detail: str) -> None:
        super().__init__("forbidden", "Forbidden", 403, detail)


@cache
def jwks_client() -> jwt.PyJWKClient:
    url = (
        f"{settings.KEYCLOAK_INTERNAL_URL.rstrip('/')}/realms/{settings.KEYCLOAK_REALM}"
        "/protocol/openid-connect/certs"
    )
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def validate_token(token: str) -> dict[str, Any]:
    try:
        key = jwks_client().get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.KEYCLOAK_CLIENT_ID,
            issuer=settings.KEYCLOAK_ISSUER,
            leeway=settings.JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWKClientError as exc:
        raise Unauthorized(f"signing key: {exc}") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized(str(exc)) from exc
    if claims.get("azp") != settings.KEYCLOAK_CLIENT_ID:
        raise Unauthorized("azp does not match the client")
    if claims.get("typ") != "Bearer":
        raise Unauthorized("not an access token")
    return claims


class KeycloakAdmin(HttpBearer):
    def authenticate(self, request: HttpRequest, token: str) -> dict[str, Any]:
        claims = validate_token(token)
        roles = (claims.get("realm_access") or {}).get("roles") or []
        if ADMIN_ROLE not in roles:
            raise Forbidden("the admin realm role is required")
        return claims
