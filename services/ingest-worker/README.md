# Ingest worker

Builds the catalog. It pulls anime data from AniList, Shikimori, and Annict, links the same anime across sources, merges the best field from each, fills language gaps, and writes the result to Postgres, Elasticsearch, and Redis.

It's the only part of the system that talks to external anime APIs, which keeps rate limits and source quirks in one place. The work runs as Celery tasks on a schedule. A small Django app on top gives an admin site for inspecting and correcting data, and an internal django-ninja API for starting runs and checking on them.

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Packaging | uv (`pyproject.toml`, `uv.lock`) |
| Framework | Django 5.2 LTS |
| Internal HTTP API | django-ninja |
| Background jobs | Celery 5 with Redis as broker |
| Schedules | django-celery-beat (schedules editable in the admin) |
| Task results | django-celery-results (stored in the `ingest` schema) |
| Monitoring | Flower (local only) |
| HTTP client | httpx (sync, inside tasks) |
| Validation | pydantic v2 models per source |
| Search | elasticsearch-py (bulk helpers) |
| Redis | redis-py |
| Romaji | cutlet (Hepburn), pykakasi as fallback |
| Markup cleanup | selectolax for HTML, regex for Shikimori tags |
| Auth for the ninja API | PyJWT with `PyJWKClient`, Keycloak public keys |
| Tests | pytest, pytest-django, respx, Testcontainers |
| Lint and types | ruff, mypy with django-stubs and celery-types |

## Processes

One codebase, three processes, all from the same image:

| Compose service | Command | Role |
|---|---|---|
| `ingest-web` | `gunicorn ingest.asgi -k uvicorn.workers.UvicornWorker` | Django admin at `/admin`, ninja API at `/api/v1` |
| `ingest-worker` | `celery -A ingest worker -Q pipeline,source.anilist,source.shikimori,source.annict,index` | Runs tasks |
| `ingest-beat` | `celery -A ingest beat -S django_celery_beat.schedulers:DatabaseScheduler` | Fires scheduled runs |
| `flower` (local only) | `celery -A ingest flower` | Task monitoring UI |

`ingest-web` listens on 8082 inside the network and is published only locally. Nothing public calls it.

## Postgres schemas

The ingest worker owns two schemas in the shared `anime` database and migrates both with Django migrations. It connects as the `ingest_svc` role, which owns them.

| Schema | Django connection alias | Contents |
|---|---|---|
| `catalog` | `catalog` | The anime data: `anime`, `anime_localization`, synonyms, genres, tags, studios, relations, `raw_source_record`, `anime_override` |
| `ingest` | `default` | Django internals (admin users, sessions, content types), Celery beat schedules and task results, `IngestRun`, `RunStage` |

Both aliases point to the same database with a different `search_path` (`catalog, public` and `ingest, public`), and `ingest/db_router.py` sends the `catalog` app to the `catalog` alias and everything else to `default`. Each schema gets its own `django_migrations` table.

```bash
uv run python manage.py migrate --database=catalog   # catalog app only
uv run python manage.py migrate                      # everything else, into ingest
```

The `ingest-migrate` compose service runs both, and must finish before the API and rec engine start, since their foreign keys point into `catalog`.

### Catalog is a contract

The API and the rec engine read `catalog` directly (read-only, enforced by their roles). That makes the catalog tables a contract:

- `contract/catalog-schema.sql` is a schema-only dump of `catalog` (`pg_dump --schema-only --schema=catalog`), regenerated with `manage.py dump_catalog_contract` and committed with every catalog migration. The other services load it in their tests.
- A test fails if the committed contract differs from what the migrations produce, so it can't go stale.
- Adding tables or columns is always safe. Renaming or dropping anything goes in three separate changes: add the new column and fill it, switch the readers, then drop the old one.

## Layout

```
services/ingest-worker/
├── pyproject.toml
├── manage.py
├── contract/
│   └── catalog-schema.sql         schema-only dump of `catalog`, read by other services' tests
├── ingest/                        Django project
│   ├── settings.py                django-environ, both DB aliases (same DB, different search_path), Celery config
│   ├── celery.py                  Celery app, queues, routes
│   ├── db_router.py               catalog app → `catalog` alias, everything else → `default`
│   ├── api.py                     NinjaAPI instance, JWT auth, router mounting
│   └── asgi.py
├── catalog/                       models and migrations for schema `catalog` + admin
│   ├── models.py
│   └── admin.py
├── runs/                          IngestRun, RunStage models, ninja routes, admin
├── pipeline/
│   ├── tasks.py                   Celery tasks, one per stage
│   ├── orchestration.py           builds the Celery canvas for a run
│   ├── http.py                    shared client: User-Agent, Redis token bucket, retries
│   ├── sources/
│   │   ├── anilist.py
│   │   ├── shikimori.py
│   │   └── annict.py
│   ├── idmap/
│   │   ├── arm.py
│   │   └── manami.py
│   ├── merge/
│   │   ├── precedence.py
│   │   └── cleanup.py
│   ├── localize/
│   │   ├── fallback.py
│   │   └── romaji.py
│   └── sinks/
│       ├── postgres.py
│       ├── elasticsearch.py
│       └── redis_pools.py
├── vocab/
│   ├── genres.yaml
│   └── tags.yaml
├── data/
│   └── manami-snapshot.json.gz    last release before the upstream archive
└── tests/
    └── fixtures/                  recorded source responses
```

## Running

```bash
# from services/ingest-worker, with postgres, redis, elasticsearch running
uv sync
uv run python manage.py migrate --database=catalog    # schema catalog
uv run python manage.py migrate                       # schema ingest
uv run python manage.py createsuperuser               # for the admin site
uv run python manage.py runserver 8082                # admin + API
uv run celery -A ingest worker -l info -Q pipeline,source.anilist,source.shikimori,source.annict,index
uv run celery -A ingest beat -l info -S django_celery_beat.schedulers:DatabaseScheduler

# management commands (enqueue a run and wait for it)
uv run python manage.py ingest_run --full --limit 500
uv run python manage.py ingest_run --incremental
uv run python manage.py ingest_refetch anilist --ids 16498
uv run python manage.py ingest_merge                  # re-merge from raw_source_record, no network
uv run python manage.py ingest_index                  # full Elasticsearch rebuild with alias swap
uv run python manage.py ingest_pools
uv run python manage.py ingest_vocab                  # sync YAML vocabulary into Postgres

uv run pytest
uv run ruff check . && uv run mypy .
```

From the repo root: `docker compose run --rm ingest-migrate`, then `docker compose up -d ingest-web ingest-worker ingest-beat`, then `docker compose exec ingest-web python manage.py ingest_run --full --limit 500`.

`--limit N` takes the top N by AniList popularity, which gives a realistic dev catalog in a few minutes. Add `--no-wait` to enqueue and return immediately.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | – | Required |
| `DJANGO_DEBUG` | `false` | |
| `DJANGO_ALLOWED_HOSTS` | `ingest-web,localhost` | |
| `POSTGRES_URL` | `postgresql://ingest_svc:…@postgres:5432/anime` | Both aliases; `ingest_svc` owns schemas `catalog` and `ingest` |
| `ELASTICSEARCH_URL`, `REDIS_URL` | see infra | Sinks |
| `CELERY_BROKER_URL` | `redis://redis:6379/1` | Separate Redis DB from the app cache |
| `ES_INDEX_VERSION` | `1` | Suffix for the index created by a full reindex |
| `HTTP_USER_AGENT` | see infra | Sent on every request; Shikimori rejects requests without one |
| `ANILIST_URL` | `https://graphql.anilist.co` | |
| `SHIKIMORI_URL` | `https://shikimori.io/api` | The old `shikimori.one` domain may still appear in older docs |
| `ANNICT_URL` | `https://api.annict.com/graphql` | |
| `ANNICT_TOKEN` | – | Personal access token; without it, Annict is skipped with a warning |
| `RATE_ANILIST_PER_MIN` | `30` | Conservative defaults, well under each API's limits |
| `RATE_SHIKIMORI_PER_MIN` | `60` | |
| `RATE_ANNICT_PER_MIN` | `30` | |
| `INCREMENTAL_DAYS` | `3` | How far back an incremental run looks |
| `KEYCLOAK_INTERNAL_URL`, `KEYCLOAK_ISSUER`, `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID` | same as the API | For validating tokens on the ninja API |

## Celery design

### Queues

| Queue | Tasks | Worker concurrency |
|---|---|---|
| `pipeline` | Run orchestration, ID mapping, merge, localize, Postgres writes | 4 |
| `source.anilist` | AniList fetch tasks | 1 |
| `source.shikimori` | Shikimori fetch tasks | 1 |
| `source.annict` | Annict fetch tasks | 1 |
| `index` | Elasticsearch and Redis pool writes | 1 |

Locally one worker consumes all queues. In a bigger setup, each source queue can get its own worker without code changes.

### Rate limiting

Celery's built-in `rate_limit` applies per worker process, so it isn't enough once more than one worker runs. Every outgoing request instead takes a token from a Redis token bucket shared by all workers (`ratelimit:source:{name}`, refilled at `RATE_{SOURCE}_PER_MIN`). When no token is available, the task sleeps up to 2 seconds and then reschedules itself with `self.retry(countdown=…)` instead of blocking a worker slot.

### Retries

- Network errors and 5xx: `autoretry_for` with exponential backoff and jitter, up to 6 attempts.
- HTTP 429: `self.retry(countdown=Retry-After)`, not counted against the attempt limit.
- 4xx other than 429: fail the item, record it on the run, continue with the rest.

### Reliability settings

- `acks_late = True` and `task_reject_on_worker_lost = True`: a task killed mid-way is redelivered.
- Every task is idempotent (upserts, hash checks, alias swaps), so redelivery is safe.
- Task arguments are IDs and small dicts only. Payloads travel through `raw_source_record`, never through the broker.
- A Redis lock (`lock:ingest:run`, 6-hour timeout) prevents two runs overlapping. A second run request while one is active returns `409 run-in-progress`.

### Run shape

A run is one Celery canvas built in `pipeline/orchestration.py`:

```
start_run
 → group(fetch AniList pages)                       queue source.anilist
 → map_ids
 → group(fetch Shikimori batch, fetch Annict batch) queues source.shikimori / source.annict
 → chord → merge_and_localize (batches of 200)      queue pipeline
 → write_postgres
 → index_elasticsearch                              queue index
 → rebuild_pools                                    queue index
 → finish_run
```

Each stage updates its `RunStage` row (pending, running, done, failed, counts, timing), which is what the admin and the API show. A failed stage marks the run failed; earlier stages' output stays, and a new run skips unchanged records by payload hash.

### Schedules

Seeded by a data migration, editable in the admin under Periodic tasks:

| Schedule | Task |
|---|---|
| Daily 02:00 UTC | Incremental run |
| Sunday 03:00 UTC | Full run with Elasticsearch rebuild |
| Weekly | Refresh the arm ID-mapping file |

The rec engine's nightly jobs are scheduled after the ingest window (see `services/rec-engine/README.md`).

## Internal API (django-ninja)

Base path `/api/v1`. OpenAPI docs at `/api/v1/docs`. Every endpoint except `/healthz` requires a Keycloak access token with the `admin` realm role, validated with the same rules as the main API (signature via Keycloak's public keys, `iss`, `aud`, `azp`, `exp`, `typ`).

| Method and path | Purpose |
|---|---|
| `POST /runs` | Start a run: `{ "mode": "full" \| "incremental", "limit": 500, "sources": ["anilist", "shikimori"] }` → `202 { "runId": 42 }` |
| `GET /runs?status=running` | List runs, newest first |
| `GET /runs/{id}` | Run detail with every stage's status, counts, timing, and item errors |
| `POST /runs/{id}/cancel` | Revoke remaining tasks; finished stages stay |
| `POST /anime/{anilistId}/refresh` | Refetch all sources for one anime, then merge, write, and index it |
| `GET /anime/{anilistId}/merge-preview` | Every merged field with its chosen value, source, and the losing candidates |
| `GET /sources` | Per source: last success, error rate over 24 h, 429s over 24 h, current bucket level |
| `POST /index/rebuild` | Full Elasticsearch rebuild with alias swap |
| `POST /pools/rebuild` | Rebuild Redis pools |
| `POST /vocab/sync` | Load the YAML vocabulary into Postgres |
| `GET /healthz` | Liveness; checks both databases, Redis, and the broker |

Errors use the same `application/problem+json` shape as the main API, with types such as `run-in-progress`, `run-not-found`, `anime-not-found`, `unauthorized`, and `forbidden`.

## Admin site

Available at `/admin` on `ingest-web`, with local Django superusers (not Keycloak accounts).

- **Anime:** search by any title, see localizations with source and machine flags, see raw payloads per source side by side. Catalog data is read-only here except overrides.
- **Overrides:** fix a wrong value permanently. An `anime_override` row (`anime_id`, `field`, `locale`, `value`, `note`) beats every source during merge. Saving an override enqueues a refresh of that anime.
- **Runs:** stage progress, counts, item errors, links to Flower.
- **Periodic tasks:** enable, disable, or reschedule runs.
- **Vocabulary:** read-only view of genres and tags. The YAML files stay the source of truth, so translations are reviewed in git.

## Pipeline stages

### 1. Fetch

- **AniList** is the backbone. Full mode pages through every anime sorted by ID, one task per page. Incremental mode fetches anime updated in the last `INCREMENTAL_DAYS` plus everything currently airing.
- **Shikimori** is fetched by MAL ID for every anime that has one. Its IDs are the same as MAL's for almost all entries; some entries use a letter-prefixed ID (for example `z36098`), so `shikimori_id` is stored as text.
- **Annict** is fetched by Annict ID from the ID map, in GraphQL batches.
- Every response is stored in `raw_source_record` with a payload hash before anything else happens. Unchanged hashes skip downstream work.

### 2. Map IDs

```
AniList id ──(AniList's idMal)──> MAL id ──(same id)──> Shikimori
     └──────────────(arm)──────────────> Annict id
```

`manami-snapshot.json.gz` fills gaps where AniList has no `idMal`. The snapshot is frozen because the upstream project was archived in July 2026; new anime rely on AniList and arm only. When sources disagree on a mapping, AniList's `idMal` wins and the conflict is logged on the run.

### 3. Merge

Field precedence, first non-empty wins. An `anime_override` for the field always comes first.

| Field | Sources in order | Notes |
|---|---|---|
| `title.en` | AniList english | |
| `title.ru` | Shikimori `license_name_ru`, Shikimori `russian` | Official Russian license title first |
| `title.ja` | AniList native, Annict title | |
| `title.ja_latn` | AniList romaji, generated | See localize |
| `synopsis.en` | AniList description | |
| `synopsis.ru` | Shikimori description | |
| `synopsis.ja` | – | No open source; stays empty unless machine translation is enabled |
| `synonyms` | AniList synonyms ∪ Shikimori synonyms ∪ English/Japanese titles from other sources | Deduplicated case-insensitively |
| `score` | AniList averageScore ÷ 10 | Falls back to meanScore |
| `format`, `status`, `episodes`, `season`, `duration` | AniList | |
| `genres` | AniList genres mapped through `vocab/genres.yaml` | Unknown genre → skipped and logged |
| `tags` | AniList tags with rank ≥ 60 mapped through `vocab/tags.yaml` | Spoiler tags stored but flagged |
| `studios`, `relations`, `cover_url`, `is_adult` | AniList | |

Cleanup rules in `pipeline/merge/cleanup.py`:

- AniList descriptions: convert `<br>` to newlines, strip other tags, remove trailing "(Source: …)" credit lines.
- Shikimori descriptions: replace `[character=…]Name[/character]` and similar tags with their inner text, drop `[spoiler]` blocks entirely.
- Trim whitespace, collapse repeated blank lines, drop text shorter than 20 characters as noise.

### 4. Localize

- `title.ja_latn` missing but `title.ja` present → generate with cutlet (Hepburn, foreign words kept in their English spelling), set `title_machine = true`, `title_source = 'cutlet'`. If cutlet fails, try pykakasi.
- Nothing else is generated by default. Fallback between languages happens at read time in the API, so storage keeps only real or explicitly generated text.
- Optional machine translation (`--translate ru,ja` on `ingest_run`) is off by default. When on, it fills `synopsis.ru` and `synopsis.ja` from English and sets `synopsis_machine = true`. Real text always replaces machine text on a later run; machine text never replaces real text.

### 5. Write Postgres

Upserts keyed by `anilist_id`, one transaction per batch of 200 anime, through the `catalog` alias. Rows for anime that disappear from AniList are kept and marked `status = 'REMOVED'`, so user history never points at nothing.

### 6. Index Elasticsearch

- Full run or `ingest_index`: create `anime_v{ES_INDEX_VERSION}` from `infra/elasticsearch/anime-index.json`, bulk-load all anime, refresh, atomically move the `anime` alias, delete the previous index.
- Incremental run: bulk upsert changed documents into the index behind the alias.

The document shape is defined in `infra/README.md`.

### 7. Rebuild pools

Rebuilds every `pool:*` set listed in `infra/README.md`. Each set is written to `{key}:next` and renamed over the live key. Adult titles and `REMOVED` titles are never pooled.

## Vocabulary

`vocab/genres.yaml` and `vocab/tags.yaml` map source genre and tag names to stable slugs and hold the translated names:

```yaml
- slug: slice-of-life
  sources: { anilist: "Slice of Life" }
  names:
    en: Slice of Life
    ru: Повседневность
    ja: 日常
    ja_latn: Nichijō
```

Adding a genre or fixing a translation is a YAML edit plus `ingest_vocab`. No reindex is needed because Elasticsearch stores slugs only.

## Testing

- Source clients are tested against recorded responses in `tests/fixtures/` with respx; tests never hit real APIs.
- Merge and cleanup rules have table-driven tests, one case per row of the precedence table, plus override cases.
- Task logic lives in plain functions that tasks call, so most tests call the functions directly without Celery.
- Canvas tests run with `task_always_eager = True` to check stage order and `RunStage` updates.
- An end-to-end test starts Postgres, Redis, and Elasticsearch in Testcontainers, runs a real Celery worker in a thread, runs the whole pipeline on 20 fixture anime, and asserts on stored rows, the alias target, and pool contents.
- A test fails if `makemigrations --check` finds model changes without a migration.
- A test fails if `contract/catalog-schema.sql` differs from a fresh dump of the migrated `catalog` schema.
- ninja endpoints are tested with signed test JWTs, including role checks.

## Rules

- Be a polite client: always send `HTTP_USER_AGENT`, go through the shared token bucket, never raise rate defaults above a source's documented limits, always honor `Retry-After`.
- Raw responses go into `raw_source_record` before any processing, so a merge bug can be fixed and re-run without refetching.
- Real text beats machine text, and overrides beat everything.
- This service owns schemas `catalog` and `ingest` and is the only one that migrates or writes them. Every catalog migration ships with a regenerated `contract/catalog-schema.sql`.
- Never rename or drop a catalog column in one step; other services read it.
- Writes only schemas `catalog` and `ingest`, Elasticsearch, `pool:*`, `ratelimit:source:*`, and `lock:ingest:*` keys. Never touches user tables.
- Tasks take IDs, not payloads, and are idempotent.
- Adding a source means: a module in `pipeline/sources/`, a queue in `ingest/celery.py`, a row in the precedence table here, and a credit line in the root README.
