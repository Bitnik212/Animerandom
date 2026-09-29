# Infrastructure

Shared infrastructure for all services: Postgres (source of truth), Elasticsearch (search), Redis (cache and random pools), and Keycloak (identity). This folder holds the Postgres bootstrap (roles, schemas, grants), the search index definition, the Redis key layout, and the Keycloak realm. Table migrations live in the service that owns each schema. Nothing here runs business logic.

```
infra/
├── README.md
├── postgres/
│   └── init/                  runs on first container start: databases, roles,
│                              schemas, grants, extensions
├── elasticsearch/
│   ├── Dockerfile             adds analysis-kuromoji and analysis-icu plugins
│   └── anime-index.json       settings + mappings for anime_v{N}
└── keycloak/
    ├── realm-anime-picker.json   realm export, imported on first start
    └── themes/anime-picker/      email templates in the app languages
```

## Local stack

`docker-compose.yml` at the repo root starts everything.

| Service | Image | Notes |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | pgvector preinstalled. Hosts two databases: `anime` (schemas `catalog`, `ingest`, `app`, `rec`) and `keycloak`. Data in the `pgdata` volume. |
| `ingest-migrate` | ingest worker image | One-shot. Migrates the `catalog` and `ingest` schemas. The API and rec engine wait for it, then migrate their own schemas on start. |
| `elasticsearch` | built from `infra/elasticsearch/Dockerfile` | Single node, security disabled (local only). Needs ~2 GB heap. |
| `redis` | `redis:7` | No persistence required; everything in it can be rebuilt. |
| `keycloak` | `quay.io/keycloak/keycloak` (pin the version in `.env`) | `start-dev --import-realm` locally. Only the API calls it; port 8180 on the host is for the admin console. |

Common commands:

```bash
docker compose up -d postgres elasticsearch redis keycloak
docker compose run --rm ingest-migrate               # catalog + ingest schemas
docker compose exec postgres psql -U anime anime     # SQL shell as the admin role
\dn+                                                 # (in psql) schemas, owners, grants
curl localhost:9200/_cat/aliases?v                   # which index is live
docker compose exec redis redis-cli --scan --pattern 'pool:*' | head
docker compose down -v                               # wipe everything, including data
```

## Configuration

All keys live in `.env` (copy from `.env.example`).

| Key | Default | Used by |
|---|---|---|
| `POSTGRES_URL` | `postgresql://{role}:{password}@postgres:5432/anime`, with a different role per service | api (`api_svc`), rec-engine (`rec_svc`), ingest-worker (`ingest_svc`) |
| `POSTGRES_ADMIN_USER`, `POSTGRES_ADMIN_PASSWORD` | `anime` / `anime` | postgres container and `init` scripts only; no service uses this role |
| `API_DB_PASSWORD`, `REC_DB_PASSWORD`, `INGEST_DB_PASSWORD` | dev values in `.env.example` | used by `init` to create the service roles |
| `ELASTICSEARCH_URL` | `http://elasticsearch:9200` | api, ingest-worker |
| `ES_VERSION` | pin an exact version | Dockerfile build arg |
| `ES_INDEX_VERSION` | `1` | ingest-worker (creates `anime_v{N}`) |
| `REDIS_URL` | `redis://redis:6379/0` | api, ingest-worker |
| `CELERY_BROKER_URL` | `redis://redis:6379/1` | ingest-worker; broker kept in a separate Redis DB |
| `REC_ENGINE_URL` | `http://rec-engine:8081` | api |
| `ANNICT_TOKEN` | – | ingest-worker |
| `HTTP_USER_AGENT` | `anime-picker/0.1 (+https://github.com/<you>/anime-picker)` | ingest-worker |
| `KC_VERSION` | pin an exact version | keycloak image tag |
| `KC_HOSTNAME` | `http://localhost:8180` | keycloak; fixes the `iss` claim of every token |
| `KC_DB_URL` | `jdbc:postgresql://postgres:5432/keycloak` | keycloak |
| `KC_BOOTSTRAP_ADMIN_USERNAME`, `KC_BOOTSTRAP_ADMIN_PASSWORD` | `admin` / `admin` | keycloak master admin, for the admin console only |
| `KEYCLOAK_*` | see `services/api/README.md` | api |

## Postgres

### Schemas and roles

One database, `anime`, split into schemas. Each schema has exactly one owner service. The owner creates and migrates its tables with its own tool and is the only one that can write to them; other services get `SELECT` where they need it. Postgres enforces this: each service connects with its own role, and roles have no rights outside what's granted below.

| Schema | Owner role | Migrated by | Readable by |
|---|---|---|---|
| `catalog` | `ingest_svc` | ingest-worker, Django migrations | `api_svc`, `rec_svc` |
| `ingest` | `ingest_svc` | ingest-worker, Django migrations | – |
| `app` | `api_svc` | api, Flyway | `rec_svc` |
| `rec` | `rec_svc` | rec-engine, Alembic | – |

`postgres/init` runs once, on an empty data volume:

```sql
CREATE ROLE ingest_svc LOGIN PASSWORD :'ingest_password';
CREATE ROLE api_svc    LOGIN PASSWORD :'api_password';
CREATE ROLE rec_svc    LOGIN PASSWORD :'rec_password';

CREATE EXTENSION IF NOT EXISTS vector;     -- in public, usable by every role
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA catalog AUTHORIZATION ingest_svc;
CREATE SCHEMA ingest  AUTHORIZATION ingest_svc;
CREATE SCHEMA app     AUTHORIZATION api_svc;
CREATE SCHEMA rec     AUTHORIZATION rec_svc;

-- catalog is read by api and rec-engine; REFERENCES allows their foreign keys
GRANT USAGE ON SCHEMA catalog TO api_svc, rec_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE ingest_svc IN SCHEMA catalog
  GRANT SELECT, REFERENCES ON TABLES TO api_svc, rec_svc;

-- rec-engine reads user signals from app
GRANT USAGE ON SCHEMA app TO rec_svc;
ALTER DEFAULT PRIVILEGES FOR ROLE api_svc IN SCHEMA app
  GRANT SELECT ON TABLES TO rec_svc;

ALTER ROLE ingest_svc SET search_path = ingest, public;
ALTER ROLE api_svc    SET search_path = app, public;
ALTER ROLE rec_svc    SET search_path = rec, public;
```

The default privileges mean a new catalog table is readable by the API the moment the ingest worker creates it, with no grant step to forget. Code always writes schema-qualified names for tables it doesn't own (`catalog.anime`), so `search_path` only covers the service's own schema.

### Tables

**Schema `catalog` (owner: ingest-worker)**

| Table | Purpose | Key columns |
|---|---|---|
| `anime` | One row per anime | `id bigserial pk`, `anilist_id int unique not null`, `mal_id int unique`, `shikimori_id text`, `annict_id text`, `format`, `status`, `season`, `season_year`, `episodes`, `duration_min`, `score numeric(3,1)`, `popularity int`, `cover_url`, `is_adult bool`, `updated_at` |
| `anime_localization` | Title and synopsis per locale | pk `(anime_id, locale)`, `locale` check in `('en','ru','ja','ja_latn')`, `title`, `synopsis`, `title_source`, `synopsis_source`, `title_machine bool`, `synopsis_machine bool` |
| `anime_synonym` | Alternative titles for search | `anime_id`, `value`, `locale` (nullable when unknown) |
| `genre`, `genre_localization` | Controlled vocabulary, about 20 genres | `slug` unique; names per locale |
| `tag`, `tag_localization` | Controlled vocabulary, a few hundred tags | `slug` unique, `is_spoiler`; names per locale |
| `anime_genre` | Many-to-many | `(anime_id, genre_id)` |
| `anime_tag` | Many-to-many with relevance | `(anime_id, tag_id)`, `rank smallint` 0–100 |
| `studio`, `anime_studio` | Studios | `studio.name`, `anime_studio.is_main` |
| `anime_relation` | Sequel, prequel, side story… | `(anime_id, related_id, kind)` |
| `raw_source_record` | Last raw payload per source record | pk `(source, source_id)`, `payload jsonb`, `payload_hash`, `fetched_at` |
| `anime_override` | Manual corrections from the ingest admin; beat every source | `id`, `anime_id`, `field`, `locale` (nullable), `value`, `note`, `updated_by`, `updated_at`; unique `(anime_id, field, locale)` |

`raw_source_record` lets the worker re-run merging and localization without refetching anything.

**Schema `ingest` (owner: ingest-worker)**

Django's own tables (admin users, sessions, content types), Celery beat schedules, Celery task results, and run history (`runs_ingestrun`, `runs_runstage`). Private to the ingest worker.

**Schema `app` (owner: api)**

| Table | Purpose | Key columns |
|---|---|---|
| `app_user` | Everything about a user except credentials | `id uuid pk` (= Keycloak `sub`), `display_name`, `locale`, `show_adult bool default false`, `onboarding_completed_at`, `last_seen_at`, `created_at`; created at sign-up (or on the first authenticated request if missing) |
| `user_anime` | Watch status and rating | pk `(user_id, anime_id)`, `anime_id` references `catalog.anime`, `status` in `planned, watching, completed, dropped`, `score smallint` 1–10 nullable, `updated_at` |
| `user_feedback` | Negative and implicit signals | `user_id`, `anime_id`, `kind` in `not_interested, skipped`, `created_at` |

**Schema `rec` (owner: rec-engine)**

| Table | Purpose | Key columns |
|---|---|---|
| `anime_embedding` | Content vectors | pk `(anime_id, model)`, `anime_id` references `catalog.anime`, `embedding vector(384)`, `source_hash`, HNSW index with `vector_cosine_ops` |
| `rec_model` | Trained model versions | `id`, `kind` (`als`), `artifact_path`, `trained_at`, `metrics jsonb`, `is_active bool` |

### Migration rules

| Schema | Where migrations live | When they run |
|---|---|---|
| `catalog`, `ingest` | `services/ingest-worker/*/migrations/` (Django) | `ingest-migrate` one-shot, before anything else |
| `app` | `services/api/src/main/resources/db/migration/` (Flyway, `V{n}__{description}.sql`) | API startup, history table in `app` |
| `rec` | `services/rec-engine/migrations/` (Alembic) | rec engine startup, version table in `rec` |

- A service never migrates a schema it doesn't own. The roles make it impossible anyway.
- Never edit an applied migration; write a new one.
- Every migration must run on an empty schema and on the current state.
- The ingest worker's migrations run first because `app` and `rec` have foreign keys into `catalog`.
- `catalog` is a contract for its readers. The ingest worker commits a schema-only dump to `services/ingest-worker/contract/catalog-schema.sql`, and the API and rec engine load it in their tests. Adding tables and columns is always safe; renaming or dropping something readers use goes add → switch readers → drop, across separate changes.

## Elasticsearch

### Index and alias

- Physical index: `anime_v{ES_INDEX_VERSION}`.
- Everything reads and writes through the alias `anime`.
- A mapping change means: bump `ES_INDEX_VERSION`, run `manage.py ingest_index` in the ingest worker (creates the new index, bulk-loads it, swaps the alias atomically, deletes the old one).

### Analyzers

| Analyzer | Built from | Used for |
|---|---|---|
| `ru_text` | standard tokenizer, lowercase, russian stop + stemmer | Russian titles and synopses |
| `ja_text` | kuromoji tokenizer, kuromoji baseform, cjk width, lowercase | Japanese titles and synopses |
| `latn_text` | icu tokenizer, icu folding | English and romaji; folding makes `Kyōkai` match `kyokai` |
| `en_text` | built-in `english` | English synopses |

### Document shape

One document per anime, `_id` = internal `id`.

```json
{
  "id": 1024,
  "title":    { "en": "Attack on Titan", "ru": "Атака титанов", "ja": "進撃の巨人", "ja_latn": "Shingeki no Kyojin" },
  "synonyms": ["AoT", "SnK"],
  "synopsis": { "en": "…", "ru": "…", "ja": null },
  "title_suggest": ["Attack on Titan", "Атака титанов", "進撃の巨人", "Shingeki no Kyojin", "AoT"],
  "genres": ["action", "drama", "fantasy"],
  "tags": ["military", "survival"],
  "studios": ["Wit Studio"],
  "format": "TV",
  "status": "FINISHED",
  "season_year": 2013,
  "decade": "2010s",
  "length": "medium",
  "episodes": 25,
  "score": 8.5,
  "popularity": 900000,
  "is_adult": false
}
```

Field types: `title.*` and `synopsis.*` are `text` with the matching analyzer; `synonyms` uses `latn_text` plus a `ja_text` subfield; `title_suggest` is `search_as_you_type` with the same subfields; `genres`, `tags`, `studios`, `format`, `status`, `decade`, `length` are `keyword`; numbers are `integer` or `float`.

Genres and tags are stored as slugs. Localized names come from Postgres, so renaming a genre in one language never requires a reindex.

## Keycloak

Keycloak stores credentials, issues and revokes tokens, and sends account emails. Per user it holds only email, password hash, enabled flag, roles, and a `locale` attribute for emails; all other user data is in the app's `app_user` table. It keeps its data in its own `keycloak` database; the app schema never references Keycloak tables, only the user's ID (`sub`).

Clients never reach Keycloak. The API service wraps sign-up, sign-in, refresh, sign-out, and account management (see `services/api/README.md`), so Keycloak's own login and registration pages are unused and it can stay on the internal network. Port 8180 is published only so the admin console works locally.

### Realm `anime-picker`

Defined in `keycloak/realm-anime-picker.json` and imported on first start. Change settings in the admin console, then re-export (`kc.sh export --realm anime-picker`) and commit the file, so the realm is reproducible.

| Setting | Value |
|---|---|
| User registration (Keycloak page) | Off; users are created only through the Admin API by the API service |
| Login with email | On, email as username |
| Verify email | Off locally; if turned on, set `EMAIL_VERIFICATION_REQUIRED=true` in the API |
| Password policy | Length 8+, not username, not email |
| Brute-force detection | On (per-user lockout after repeated failures) |
| Realm roles | `user` (default role), `admin` |
| Access token lifespan | 5 minutes |
| SSO session idle / max | 30 days / 90 days (refresh tokens stay valid this long, mobile-friendly) |
| SMTP | Required for forgot-password and verify-email; any dev mail catcher works locally |
| Internationalization | On; supported `en`, `ru`, `ja`; used for emails only |
| Email theme | `anime-picker` |

### Client `anime-picker-api`

The only client in the realm.

| Setting | Value |
|---|---|
| Client authentication | On (confidential, secret in `KEYCLOAK_CLIENT_SECRET`) |
| Standard flow | Off |
| Direct access grants | On (password grant for sign-in) |
| Service account | On, with `realm-management/view-users` and `realm-management/manage-users` only |
| Audience mapper | Included Client Audience `anime-picker-api`, added to access tokens |

Without the audience mapper, tokens carry `aud: account` and the API rejects them.

### Issuer

Tokens carry `iss = {KC_HOSTNAME}/realms/anime-picker`. Because `KC_HOSTNAME` is fixed, the issuer stays the same no matter which URL the API uses to reach Keycloak (`http://keycloak:8080` inside the network). The API's `KEYCLOAK_ISSUER` must equal it exactly.

### Languages

The app's own screens cover all four languages, so sign-in and sign-up are fully localized regardless of Keycloak. Keycloak's language only affects emails (password reset, verification). Built-in email texts exist for English, Russian, and Japanese; the `anime-picker` theme adds a romanized Japanese bundle. If the pinned Keycloak version doesn't accept a script-subtag locale for that bundle, `ja-Latn` users get Japanese emails. The API keeps each user's `locale` attribute in sync.

## Redis

Everything in Redis is derived and can be rebuilt. Losing Redis costs latency, not data.

| Key pattern | Type | Writer | Contents | TTL |
|---|---|---|---|---|
| `pool:all` | set | ingest-worker | All non-adult anime IDs | none |
| `pool:genre:{slug}` | set | ingest-worker | IDs with that genre | none |
| `pool:format:{format}` | set | ingest-worker | `TV`, `MOVIE`, `OVA`, `ONA`, `SPECIAL` | none |
| `pool:score:{n}plus` | set | ingest-worker | Score ≥ n, for n in 6, 7, 8, 9 | none |
| `pool:decade:{decade}` | set | ingest-worker | `1990s`, `2000s`, … | none |
| `pool:length:{bucket}` | set | ingest-worker | `short` ≤ 13 eps, `medium` 14–26, `long` > 26 | none |
| `user:{uuid}:excluded` | set | api | Watched + not interested IDs | 24 h, rebuilt on miss |
| `cache:anime:{id}:{locale}` | string (JSON) | api | Localized anime card | 1 h |
| `tmp:rand:{uuid}` | set | api | Intersection scratch space | 10 s |
| `ratelimit:auth:*` | string | api | Sign-in and sign-up attempt counters | window length |
| `ratelimit:source:{name}` | hash | ingest-worker | Shared token bucket per external source | none |
| `lock:ingest:run` | string | ingest-worker | Prevents overlapping runs | 6 h |

Redis DB 1 is the Celery broker for the ingest worker. It holds queued tasks only and is not covered by the table above.

Pools are rebuilt by writing to `{key}:next` and then `RENAME`-ing over the live key, so readers never see a half-built set. Adult titles are never added to pools; the API handles them through Elasticsearch when a user has opted in.
