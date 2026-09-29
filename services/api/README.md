# API service

The only service clients talk to. It resolves the user's language, serves anime cards, picks random titles, runs search, stores watch status and ratings, and turns the rec engine's ranked IDs into a localized feed.

It never calls external anime APIs and never writes catalog data. Anything that isn't in Postgres, Elasticsearch, or Redis doesn't exist from this service's point of view.

## Stack

| Concern | Choice |
|---|---|
| Language | Kotlin 2.x on JDK 21 |
| HTTP | Ktor 3 (Netty engine) |
| Serialization | kotlinx.serialization |
| Database | Exposed (DSL, not DAO) + HikariCP |
| Migrations | Flyway, for the `app` schema only, applied at startup |
| Search | Official Elasticsearch Java client |
| Redis | Lettuce (coroutines API) |
| HTTP client (rec engine, Keycloak) | Ktor client (CIO) |
| Auth | `ktor-server-auth-jwt` + `com.auth0:jwks-rsa` for token validation; Keycloak token endpoint and Admin REST API through a small Ktor-client wrapper |
| DI | Koin |
| Tests | JUnit 5, Kotest assertions, Testcontainers |
| Lint | ktlint, detekt |
| Build | Gradle Kotlin DSL, version catalog in `gradle/libs.versions.toml` |

## Layout

```
services/api/
├── build.gradle.kts
├── src/main/kotlin/picker/
│   ├── Application.kt             Ktor module wiring, plugins, routing
│   ├── config/                    typed config from env
│   ├── features/
│   │   ├── auth/                  sign-up, sign-in, refresh, sign-out, password reset
│   │   ├── anime/                 details, similar
│   │   ├── random/                random picks, pool resolution
│   │   ├── search/                search, suggest, facets
│   │   ├── users/                 profile, statuses, ratings, feedback, watchlist
│   │   ├── recommendations/       feed assembly from rec-engine results
│   │   └── meta/                  genres, tags
│   ├── i18n/                      locale resolution, fallback chains, message bundles
│   ├── auth/                      JWT validation, principal, role checks, Keycloak client, rate limits
│   │                              (routes for /v1/auth live in features/auth/)
│   ├── infra/
│   │   ├── db/                    Exposed table objects, transactions
│   │   ├── es/                    client, query builders
│   │   ├── redis/                 key helpers matching infra/README.md
│   │   └── rec/                   rec-engine client
│   └── errors/                    problem+json mapping
├── src/main/resources/
│   ├── application.conf
│   ├── db/migration/              Flyway migrations for schema `app` (V1__init.sql, …)
│   └── messages/                  messages_en.properties, _ru, _ja, _ja_Latn
└── src/test/kotlin/picker/
```

Each folder under `features/` contains `Routes.kt` (HTTP only), `Service.kt` (logic), and `Repository.kt` or query builders (storage). Routes never touch storage directly; services never see Ktor types.

## Running

```bash
# from services/api, with postgres, elasticsearch, redis, keycloak, rec-engine running
./gradlew run                    # http://localhost:8080
./gradlew test                   # unit + integration tests (see Testing for what they need)
./gradlew ktlintCheck detekt     # must pass before merging (detekt checks src/main only)
./gradlew installDist            # build used by the Dockerfile
```

Or from the repo root: `docker compose up -d api`.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | HTTP port |
| `POSTGRES_URL` | `postgresql://api_svc:…@postgres:5432/anime` | Connects as `api_svc`: owns schema `app`, read-only on `catalog` |
| `ELASTICSEARCH_URL` | see infra | Search |
| `REDIS_URL` | see infra | Cache and pools |
| `REC_ENGINE_URL` | `http://rec-engine:8081` | Rec engine base URL |
| `REC_ENGINE_TIMEOUT_MS` | `800` | After this, fall back to popularity |
| `DEFAULT_LOCALE` | `en` | Used when nothing else resolves |
| `KEYCLOAK_INTERNAL_URL` | `http://keycloak:8080` | Where the API reaches Keycloak (public keys, token endpoint, Admin API) |
| `KEYCLOAK_ISSUER` | `http://localhost:8180/realms/anime-picker` | Must equal the `iss` claim byte for byte |
| `KEYCLOAK_REALM` | `anime-picker` | |
| `KEYCLOAK_CLIENT_ID` | `anime-picker-api` | The one client used for token calls and the Admin API; also the required `aud` and `azp` |
| `KEYCLOAK_CLIENT_SECRET` | – | Secret of that client |
| `AUTH_RATE_LIMIT_IP` | `20/min` | Sign-in, sign-up, and forgot-password attempts per IP |
| `AUTH_RATE_LIMIT_EMAIL` | `5/min` | Sign-in attempts per email |
| `EMAIL_VERIFICATION_REQUIRED` | `false` | Matches the realm's verify-email setting |
| `JWT_LEEWAY_SECONDS` | `30` | Clock skew tolerance for `exp` and `nbf` |
| `DB_POOL_SIZE` | `10` | Hikari pool size |
| `RUN_MIGRATIONS` | `true` | Apply Flyway migrations for `app` on start |
| `TRUST_FORWARDED_HEADERS` | `false` | Set to `true` only behind a reverse proxy. The client IP (used by the per-IP rate limits) is then the address that proxy reports (`X-Forwarded-For`, last hop). With `false`, forwarded headers are ignored, so clients can't choose their own IP. |

## Authentication

Keycloak (realm `anime-picker`, see `infra/README.md`) stores credentials and issues tokens. It holds only what login needs: email, password hash, enabled flag, roles, and a `locale` attribute for emails. Every other piece of user data lives in the app's own `app_user` table.

The Admin API creates and manages users but cannot check a password or issue user tokens; only the token endpoint does that. That's why sign-up uses the Admin API and sign-in uses the token endpoint. Clients never talk to Keycloak: sign-up, sign-in, refresh, and sign-out all go through this service, so every client gets the same auth screens in all four app languages and Keycloak can stay on the internal network.

Two Keycloak interfaces are used, both through one confidential client, `anime-picker-api`:

- **Token endpoint** (`/realms/{realm}/protocol/openid-connect/token`): password grant for sign-in, refresh-token grant for refresh.
- **Admin REST API** (`/admin/realms/{realm}/...`): creating users and account management. The client's service account has only the `realm-management` roles `view-users` and `manage-users`.

Incoming requests are authorized purely by verifying the access token with Keycloak's public keys. No request to Keycloak happens on the normal request path.

### Sign-up

`POST /v1/auth/signup` with `{ "email", "password", "displayName", "locale" }`:

1. Validate input locally (email format, password length) for fast, localized errors.
2. Get a service-account token (client-credentials grant, cached until 30 s before expiry).
3. `POST /admin/realms/{realm}/users` with `username = email`, `email`, `enabled: true`, `attributes.locale`, and `credentials: [{ "type": "password", "value": …, "temporary": false }]`. New users get the default realm role `user`.
4. Keycloak returns `409` for an existing email → `409 email-taken`. A password-policy violation → `400 weak-password`.
5. If email verification is on, `PUT /admin/realms/{realm}/users/{id}/send-verify-email`.
6. Sign the user in (the sign-in steps below) and return tokens, so sign-up ends logged in. If verification is required, return `201` with `{ "verificationRequired": true }` and no tokens instead.
7. Insert the `app_user` row with `id` = the new Keycloak user ID (from the `Location` header of step 3), `display_name`, and `locale`. `displayName` is never sent to Keycloak.

If step 7 fails, the Keycloak user is deleted again, so a half-created account never exists.

### Sign-in, refresh, sign-out

| Endpoint | Keycloak call | Returns |
|---|---|---|
| `POST /v1/auth/signin` `{ email, password }` | Token endpoint, `grant_type=password`, `scope=openid` | Tokens |
| `POST /v1/auth/refresh` `{ refreshToken }` | Token endpoint, `grant_type=refresh_token` | Tokens |
| `POST /v1/auth/signout` `{ refreshToken }` | `/protocol/openid-connect/logout` with the refresh token | `204` |

All token calls authenticate as `anime-picker-api` with its client secret. The token response is passed through in camelCase:

```json
{ "accessToken": "eyJ…", "expiresIn": 300, "refreshToken": "eyJ…", "refreshExpiresIn": 2592000, "tokenType": "Bearer" }
```

Keycloak's `invalid_grant` becomes `401 invalid-credentials` with one generic message for both wrong password and unknown email. "Account is not fully set up" and "Account disabled" become `403 account-not-ready`.

### Account management

| Endpoint | Keycloak call |
|---|---|
| `POST /v1/auth/password/forgot` `{ email }` | `GET /users?email={email}&exact=true`, then `PUT /users/{id}/execute-actions-email` with `["UPDATE_PASSWORD"]`. Always returns `202`, whether the email exists or not. Needs SMTP configured in the realm. |
| `PUT /v1/users/me/password` `{ currentPassword, newPassword }` | Verify `currentPassword` with a password grant, then `PUT /users/{id}/reset-password` |
| `PATCH /v1/users/me` with `locale` | `GET` then `PUT /users/{id}` with updated `attributes.locale`, so Keycloak emails use the same language |
| `POST /v1/users/me/logout-all` | `POST /users/{id}/logout` |
| `DELETE /v1/users/me` | Delete app rows, then `DELETE /users/{id}` |
| `PUT` / `DELETE /v1/admin/users/{id}/admin` | `POST` / `DELETE /users/{id}/role-mappings/realm` with the `admin` role |

Keycloak's user update replaces the whole `attributes` map, so the locale sync always reads the current user, changes one key, and writes the user back.

Account deletion order: delete the user's rows in one transaction (`user_anime`, `user_feedback`, `app_user`), drop `user:{id}:*` keys in Redis, then delete the Keycloak user. Every step is idempotent, so on failure the API returns `502 identity-provider-unavailable` and the client retries.

### Token validation

Every protected request is checked locally with Ktor's `jwt` auth provider. A token is accepted only if all of these hold:

| Check | Rule |
|---|---|
| Signature | RS256, verified with the key whose `kid` matches, from `{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs` |
| `iss` | Equals `KEYCLOAK_ISSUER` |
| `aud` | Contains `anime-picker-api` (added by an audience mapper; Keycloak's default `aud` is `account`) |
| `azp` | Equals `anime-picker-api` |
| `exp`, `nbf` | Valid within `JWT_LEEWAY_SECONDS` |
| `typ` | `Bearer` (rejects ID tokens and refresh tokens) |

Public keys are cached (up to 10 keys for 24 hours, at most 10 refetches per minute). An unknown `kid` triggers one refetch, which picks up Keycloak key rotation without a restart.

### Principal

| Field | Claim |
|---|---|
| `id` (UUID) | `sub` |
| `email` | `email` |
| `roles` | `realm_access.roles` (`user`, `admin`) |

Everything else about the user (display name, preferences, onboarding state) is read from `app_user`, not from the token.

If a valid token arrives for a user with no `app_user` row (for example, a user created in the Keycloak console), the row is created on the spot with `INSERT … ON CONFLICT DO NOTHING`.

### Public and protected routes

- `/v1/auth/*`, catalog, random, search, and meta endpoints are public. With a valid token, catalog endpoints also hide watched titles and attach the user's status. An invalid or expired token on a public route is a `401`, not a silent downgrade, so client bugs surface early.
- `/v1/users/me/*` requires a valid token.
- `/v1/admin/*` requires the `admin` role.

### Abuse protection

The API is now the only door to password checks, so it rate-limits auth endpoints in Redis (`ratelimit:auth:ip:{ip}` and `ratelimit:auth:email:{sha256(email)}`, sliding window). Over the limit → `429 too-many-attempts` with `Retry-After`. Keycloak's brute-force detection stays on as a second layer; it counts failures per user, so it still works even though every request comes from the API's address.

## Locale resolution

Per request, the first match wins:

1. `lang` query parameter (`en`, `ru`, `ja`, `ja-Latn`)
2. `Accept-Language` header, best supported match
3. The user's saved `locale`
4. `DEFAULT_LOCALE`

The resolved locale goes into the `Content-Language` response header. Text fields fall back per the chains in the root README; every card reports the locale actually used for its title and synopsis. BCP 47 tags are converted to storage keys (`ja-Latn` → `ja_latn`) only in `i18n/`.

UI strings the API produces (recommendation reasons, genre and tag names) come from `messages/` bundles and the localization tables. Russian plural forms use ICU plural rules, never string concatenation.

## Endpoints

All paths are prefixed with `/v1`. Responses are JSON. Lists are paged with `page` (from 1) and `size` (default 20, max 50) unless noted.

### Anime card

The shape returned everywhere an anime appears:

```json
{
  "id": 1024,
  "title": "Атака титанов",
  "titleLocale": "ru",
  "titleNative": "進撃の巨人",
  "titleRomaji": "Shingeki no Kyojin",
  "synopsis": "…",
  "synopsisLocale": "ru",
  "synopsisMachine": false,
  "format": "TV",
  "status": "FINISHED",
  "seasonYear": 2013,
  "episodes": 25,
  "score": 8.5,
  "genres": [{ "slug": "action", "name": "Экшен" }],
  "coverUrl": "https://…",
  "userStatus": null,
  "userScore": null
}
```

List endpoints return a shorter card without `synopsis`.

### Catalog

| Method and path | Purpose |
|---|---|
| `GET /anime/{id}` | Full card plus tags, studios, and relations |
| `GET /anime/{id}/similar?limit=20` | "More like this", from the rec engine's similarity endpoint |
| `GET /anime/random` | Random pick, see below |
| `GET /meta/genres` | All genres with localized names |
| `GET /meta/tags?q=` | Tags with localized names, optional prefix filter |

### Random

`GET /anime/random`

| Param | Example | Notes |
|---|---|---|
| `genre` | `genre=action&genre=drama` | Repeatable, all must match |
| `excludeGenre` | `excludeGenre=horror` | Repeatable |
| `format` | `TV` | One of the format values |
| `minScore` | `7` | Rounded down to a pool bucket: 6, 7, 8, 9 |
| `decade` | `2010s` | |
| `length` | `short` | `short`, `medium`, `long` |
| `hideWatched` | `true` | Default `true` for authenticated requests |
| `count` | `1` | 1–10 distinct picks |
| `yearFrom`, `yearTo`, `tag` | | Not pooled; triggers the Elasticsearch path |
| `source` | `watchlist` | Pick from the user's `planned` list instead of the catalog |

Returns `{ "items": [card, …], "poolSize": 1234 }`. `poolSize` lets the client say "1,234 anime match your filters". An empty pool returns `200` with no items, not an error.

### Search

| Method and path | Purpose |
|---|---|
| `GET /search` | Full search with filters and facets |
| `GET /search/suggest?q=atta` | Up to 8 title suggestions, any script |

`GET /search` params: `q`, `genre`, `excludeGenre`, `tag`, `excludeTag`, `format`, `status`, `yearFrom`, `yearTo`, `minScore`, `episodesMin`, `episodesMax`, `hideWatched`, `sort` (`relevance` default when `q` is set, otherwise `popularity`; also `score`, `newest`), `page`, `size`.

Response:

```json
{
  "items": [ /* short cards */ ],
  "total": 312,
  "facets": {
    "genres":  [{ "slug": "action", "name": "Экшен", "count": 120 }],
    "formats": [{ "value": "TV", "count": 250 }],
    "decades": [{ "value": "2010s", "count": 140 }]
  }
}
```

### Auth

Details and Keycloak calls are in the Authentication section above.

| Method and path | Purpose |
|---|---|
| `POST /auth/signup` | Create account, returns tokens (or `verificationRequired`) |
| `POST /auth/signin` | Email and password, returns tokens |
| `POST /auth/refresh` | New tokens from a refresh token |
| `POST /auth/signout` | End the session for that refresh token |
| `POST /auth/password/forgot` | Send a reset email, always `202` |

### Current user

| Method and path | Purpose |
|---|---|
| `GET /users/me` | Profile: `id`, `email`, `displayName`, `roles`, `locale`, `showAdult` |
| `PUT /users/me/password` | Change password, see Authentication |
| `PATCH /users/me` | Update `displayName`, `showAdult`, and `locale` (only `locale` is also synced to Keycloak, for emails) |
| `DELETE /users/me` | Delete the account in the app and in Keycloak, returns `204` |
| `POST /users/me/logout-all` | End every Keycloak session of the user, returns `204` |
| `POST /users/me/onboarding` | `{ "favorites": [ids], "likedGenres": [], "dislikedGenres": [] }`; favorites are stored as `completed` with score 9 |
| `GET /users/me/anime?status=planned&sort=added` | The user's list; `sort` is `added`, `score`, `length` |
| `PUT /users/me/anime/{animeId}` | `{ "status": "completed", "score": 8 }`; `score` optional |
| `DELETE /users/me/anime/{animeId}` | Remove from list |
| `POST /users/me/feedback` | `{ "animeId": 1024, "kind": "not_interested" }` or `"skipped"` (sent on reroll) |
| `GET /users/me/recommendations?limit=20` | Personal feed |

### Admin

Requires the `admin` role.

| Method and path | Purpose |
|---|---|
| `GET /admin/users/{id}` | Keycloak user plus app stats (list size, ratings count) |
| `PUT /admin/users/{id}/admin` | Grant the `admin` role |
| `DELETE /admin/users/{id}/admin` | Revoke the `admin` role |

Recommendation items are cards plus a localized reason:

```json
{ "card": { /* short card */ }, "reason": { "code": "similar_to", "text": "Потому что вам понравилось «Врата Штейна»", "animeId": 9253 } }
```

### Operations

| Path | Purpose |
|---|---|
| `GET /healthz` | Liveness, no dependencies checked |
| `GET /readyz` | Checks Postgres, Elasticsearch, Redis, and that JWKS keys can be loaded; rec engine is reported but not required |

## Random selection

1. Map each pooled filter to a Redis key (`genre=action` → `pool:genre:action`). No filters means `pool:all`.
2. `SINTERSTORE tmp:rand:{uuid}` over those keys, then `SDIFFSTORE` away every `excludeGenre` pool and, when hiding watched, `user:{id}:excluded`. Set a 10 s TTL on the temp key.
3. `SCARD` for `poolSize`, `SRANDMEMBER` for the picks, then load cards (cache first).
4. If any non-pooled filter is present, or the user has `showAdult` enabled, skip Redis and run an Elasticsearch `function_score` query with `random_score` and the same filters.

`user:{id}:excluded` is rebuilt from Postgres on a cache miss and updated in place whenever the user changes a status or sends `not_interested`.

## Search

- A query searches every title field in every language, plus synonyms, regardless of the UI locale. Matches in the user's locale get a small boost so they rank first on ties.
- Fuzziness is `AUTO` on Latin and Cyrillic fields only; fuzzy matching on Japanese tokens produces noise.
- Facet counts come from aggregations on the same query with its own filter removed (post-filter pattern), so selecting "Action" doesn't hide the other genre counts.
- Genre and tag names in facets are localized from Postgres, cached in memory for 10 minutes.

## Recommendations

The API calls the rec engine, then hydrates and localizes results:

1. `GET {REC_ENGINE_URL}/v1/recommendations/{userId}?limit={limit}` with `REC_ENGINE_TIMEOUT_MS`.
2. Load cards for the returned IDs in one query, keeping the engine's order.
3. Turn each reason code into text using `messages/` bundles, filling in the referenced anime's title in the user's locale.
4. On timeout or error, fall back to the most popular non-excluded titles in the user's liked genres (or overall), with reason code `popular`. The client never sees an error for this endpoint because of the rec engine.

`GET /anime/{id}/similar` calls `GET {REC_ENGINE_URL}/v1/similar/{id}?limit={limit}` and adds `includeAdult=true` only for a signed-in user with `showAdult`; by default the engine leaves adult titles out.

Reason codes and their message keys:

| Code | Extra fields | Message key |
|---|---|---|
| `similar_to` | `anime_id` | `reason.similar_to` |
| `liked_by_similar_users` | – | `reason.liked_by_similar_users` |
| `popular_in_genre` | `genre` | `reason.popular_in_genre` |
| `popular` | – | `reason.popular` |

## Errors

Mapped in `errors/`. Stable `type` values:

| type | Status |
|---|---|
| `validation-failed` | 400 |
| `unauthorized` | 401 (missing, invalid, or expired token; also sets `WWW-Authenticate: Bearer`) |
| `invalid-credentials` | 401 (wrong email or password, same message for both) |
| `weak-password` | 400 (fails the realm password policy) |
| `email-taken` | 409 |
| `account-not-ready` | 403 (email not verified, or disabled) |
| `too-many-attempts` | 429 (with `Retry-After`) |
| `forbidden` | 403 (valid token, missing role) |
| `user-not-found` | 404 |
| `anime-not-found` | 404 |
| `unsupported-locale` | 400 |
| `identity-provider-unavailable` | 502 (Keycloak Admin API failed or timed out) |
| `internal` | 500 |

## Testing

- Unit tests cover locale resolution, fallback chains, message bundles, and config parsing, plus services with in-memory fakes where a feature has logic of its own.
- Integration tests run the whole application with Ktor's test host. They need:
  - A Postgres where `API_TEST_POSTGRES_URL` (default `postgresql://postgres@127.0.0.1:5433/postgres`) can create databases. Each test run creates a fresh database, loads the catalog contract (`services/ingest-worker/contract/catalog-schema.sql`) and `src/test/resources/fixtures/catalog.sql`, and lets Flyway migrate `app`.
  - A Redis at `API_TEST_REDIS_URL` (default DB 14 on local port 6380), flushed before every test.

  CI provides both as service containers.
- Keycloak's JWKS endpoint is a WireMock server with a test RSA key. Token tests break each rule in the validation table on its own: wrong `iss`, `aud` and `azp`; expired and not-yet-valid; unknown `kid`; a bad signature; ID and refresh tokens; and garbage.
- Keycloak's token endpoint and Admin REST API are WireMock stubs too (`support/KeycloakStubs.kt`). Auth tests cover:
  - sign-up (tokens, the verification-required path, `email-taken`, `weak-password`, local validation before any Keycloak call, and deleting the Keycloak user again when the `app_user` insert fails)
  - every sign-in outcome, including `502` when Keycloak errors or is unreachable
  - refresh, sign-out, and forgot-password (always `202`)
  - per-email and per-IP rate limits with `Retry-After`
  - password change, logout everywhere, and account deletion (Postgres rows, `user:{id}:*` keys, then the Keycloak user; retryable)
  - the admin view and role endpoints, the locale sync keeping other attributes, and service-account token reuse
- `RealKeycloakTest` runs the same flows end to end against a real Keycloak with the committed realm imported: signup, the password policy, the audience mapper, password change, refresh and sign-out, service-account permissions, admin view, and deletion. It's skipped unless `API_TEST_KEYCLOAK_URL` and `API_TEST_KEYCLOAK_SECRET` are set; `API_TEST_KEYCLOAK_ISSUER` defaults to `{url}/realms/anime-picker`. CI doesn't run it yet.
- The fixture catalog (`src/test/resources/fixtures/`, 12 anime) has deliberately missing translations, to exercise the fallbacks.
- The rec engine tests load this service's Flyway migrations for `app`, so a migration that breaks them fails their CI job too.
- Not done yet: Testcontainers (tests use externally provided services), a real Keycloak in CI, and the WireMock stubs for the rec engine and Elasticsearch. Those arrive with the features that use them.

## Rules

- No calls to AniList, Shikimori, Annict, or any other external data source.
- User metadata goes in `app_user`, never in Keycloak attributes. The only exception is `locale`, which Keycloak needs for emails.
- Passwords only pass through: never stored, never logged, never put in error messages. Request bodies of `/v1/auth/*` and `/v1/users/me/password` are excluded from request logging.
- The API never signs tokens and keeps no sessions. Keycloak issues and revokes; the API verifies with public keys.
- Keycloak is called only from `auth/`, only for the operations listed in the Authentication section, and never on the normal request path. The master-realm admin account is never used by the service.
- User identity comes from the validated token's `sub` only, never from a header, path, or body field.
- `KEYCLOAK_CLIENT_SECRET` is never logged, and tokens are never logged in full (log the `sub` and `jti` instead).
- Owns schema `app` and nothing else in Postgres. Catalog tables are read with schema-qualified names (`catalog.anime`) and never written; the `api_svc` role has only `SELECT` there anyway.
- A catalog column the API reads is a contract with the ingest worker. If a query needs a new column, it's added in the ingest worker first.
- No writes to Elasticsearch or `pool:*` keys.
- All user-visible text is localized here; responses never contain an untranslated message key.
- Redis key names come from `src/main/kotlin/picker/infra/redis/Keys.kt`, which mirrors the table in the repo's `infra/README.md`. No inline key strings.
- A new or changed endpoint updates the Endpoints section of this file in the same change.
