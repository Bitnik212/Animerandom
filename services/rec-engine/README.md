# Rec engine

Ranks anime for a user and finds anime similar to a given one. It combines two signals: what an anime is about (content embeddings) and what people with similar taste liked (collaborative filtering). New users with few ratings lean on content; as ratings accumulate, the collaborative signal takes over.

The engine is internal. Only the API service calls it, and it returns IDs, scores, and reason codes, never display text.

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Packaging | uv (`pyproject.toml`, `uv.lock`) |
| HTTP | FastAPI + uvicorn |
| Database | psycopg 3 + SQLAlchemy 2 Core, pgvector adapter |
| Migrations | Alembic, for the `rec` schema only, applied at startup |
| Embeddings | sentence-transformers, model `paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions). Installed by the Dockerfile with CPU-only torch and baked into the image; not in `pyproject.toml`, because the PyPI torch build bundles CUDA. |
| Collaborative filtering | `implicit` (ALS) |
| Numerics | numpy, scipy sparse |
| CLI | typer |
| Tests | pytest against a Postgres with pgvector (see Testing) |
| Lint and types | ruff, mypy (strict) |

The embedding model is multilingual, so a Russian or Japanese query vector can be compared with vectors built from English text if cross-language features are added later.

## Layout

```
services/rec-engine/
├── pyproject.toml
├── alembic.ini
├── migrations/                  Alembic revisions for schema `rec`
├── src/rec_engine/
│   ├── app.py                 FastAPI app and routes; runs migrations on start
│   ├── cli.py                 `rec` command: migrate, embed, train, evaluate
│   ├── jobs.py                embed, train, evaluate, migrate (shared by CLI and scheduler)
│   ├── scheduler.py           nightly embed + train inside the server, under an advisory lock
│   ├── config.py              pydantic-settings from env
│   ├── db.py                  engine, queries
│   ├── content/
│   │   ├── text.py            builds the text that gets embedded
│   │   └── embed.py           batch embedding job
│   ├── cf/
│   │   ├── matrix.py          user × anime interaction matrix
│   │   └── als.py             training and loading
│   ├── ranking/
│   │   ├── candidates.py      candidate generation
│   │   ├── blend.py           hybrid scoring
│   │   ├── filters.py         exclusions, sequel ordering, diversity
│   │   ├── reasons.py         reason code selection
│   │   └── recommender.py     the pipeline: candidates → blend → filters → reasons
│   └── models.py              response schemas
└── tests/
```

## Running

```bash
# from services/rec-engine
uv sync
uv run rec migrate               # migrate schema `rec` (also runs on server start)
uv run rec embed                 # embed anime that are new or changed
uv run rec train                 # train ALS and activate the new model
uv run rec evaluate              # offline metrics on a held-out split
uv run uvicorn rec_engine.app:app --port 8081 --reload
REC_TEST_POSTGRES_URL=postgresql://<role that can CREATE DATABASE>@localhost/postgres uv run pytest
uv run ruff check . && uv run mypy src
```

From the repo root, `docker compose run --rm rec-engine rec embed` runs a job and `docker compose up -d rec-engine` starts the server.

**Nightly jobs.** The server runs `embed` then `train` once a day at `NIGHTLY_AT` (UTC, default 04:30). That's after the ingest window: the daily incremental run starts at 02:00, the weekly full run at 03:00 on Sunday. A Postgres advisory lock makes sure only one replica runs it. Set `NIGHTLY_AT=` (empty) to turn it off and schedule the CLI yourself.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `PORT` | `8081` | HTTP port |
| `POSTGRES_URL` | `postgresql://rec_svc:…@postgres:5432/anime` | Connects as `rec_svc`: owns schema `rec`, read-only on `catalog` and `app` |
| `EMBEDDING_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Changing it requires re-embedding everything |
| `MODEL_DIR` | `/data/models` | ALS artifacts (mounted volume) |
| `ALS_FACTORS` | `64` | Latent dimensions |
| `ALS_ITERATIONS` | `20` | |
| `ALS_REGULARIZATION` | `0.05` | |
| `MIN_USERS_FOR_CF` | `50` | Below this many users with ratings, CF is disabled entirely |
| `MODEL_RELOAD_SECONDS` | `300` | How often the server checks for a newly activated model |
| `NIGHTLY_AT` | `04:30` | UTC time for the in-process nightly `embed` + `train`; empty disables it |
| `EMBEDDING_DIM` | `384` | Must match `EMBEDDING_MODEL` and the `vector(384)` column |

## Contract with the API

### `GET /v1/recommendations/{user_id}?limit=20`

```json
{
  "items": [
    { "anime_id": 30276, "score": 0.91, "reason": { "code": "similar_to", "anime_id": 9253 } },
    { "anime_id": 11061, "score": 0.87, "reason": { "code": "liked_by_similar_users" } },
    { "anime_id": 5114,  "score": 0.80, "reason": { "code": "popular_in_genre", "genre": "action" } }
  ],
  "model": { "als": "als-20260929T0300Z", "embedding": "paraphrase-multilingual-MiniLM-L12-v2" }
}
```

Unknown user → `200` with popularity-based items. Unknown fields must be ignored by the API so new reason fields can be added safely. A reason carries only the field of its code (`anime_id` for `similar_to`, `genre` for `popular_in_genre`); `model.als` is `null` while no ALS model is active.

### `GET /v1/similar/{anime_id}?limit=20&includeAdult=false`

```json
{ "items": [ { "anime_id": 30276, "score": 0.83 } ] }
```

Unknown anime → `404` with `type: anime-not-found`. Adult titles are left out unless `includeAdult=true`; the API sets it only for users with `showAdult`.

### `GET /healthz`

Returns `200` when the process is up. Reports whether an ALS model is loaded, but a missing model is not unhealthy; the engine falls back to content and popularity.

Reason codes (`similar_to`, `liked_by_similar_users`, `popular_in_genre`, `popular`) must stay in sync with the table in `services/api/README.md`.

## How it works

### Content embeddings

For each anime, `content/text.py` builds one English text block:

```
{title.en or title.ja_latn}. Genres: {genres}. Themes: {top tags by rank, no spoilers}. Studio: {main studio}. {synopsis.en}
```

English is used because it's the most complete synopsis language, which keeps vectors comparable across the catalog. The text is hashed; `rec embed` only re-embeds rows whose `source_hash` changed. Vectors go into `anime_embedding` with an HNSW cosine index.

Similar anime = nearest neighbors by cosine distance, excluding the anime itself and its direct relations (a sequel isn't a useful "similar" result).

### Collaborative filtering

The interaction matrix is built from `user_anime` and `user_feedback`:

| Signal | Confidence |
|---|---|
| Rated 6–10 | `1 + 4 × (score − 5) / 5` (6 → 1.8, 10 → 5) |
| Rated 1–5 | nothing: ratings of 5 or below contribute nothing positive (the formula alone would give 5 a confidence of 1) |
| Completed, no rating | 2 |
| Watching | 1.5 |
| Planned | 0.5 |
| Dropped, not interested | excluded from the matrix, used only as filters |

`rec train` fits ALS, writes `als-{timestamp}.npz` to `MODEL_DIR`, records it in `rec_model`, and marks it active. The server reloads the active model every 5 minutes. Every user's vector is recomputed at request time from their current interactions with ALS's closed-form user step (the same result as implicit's `recalculate_user`), so users created after training and ratings made since are both counted.

### Candidate generation

Up to 500 candidates from the union of:

1. Nearest neighbors of the user's top 10 rated anime (content).
2. Top ALS scores (if CF is enabled and the user has ≥ 3 interactions).
3. Most popular anime in the user's liked genres (from onboarding and ratings).

Per-source caps: 50 neighbors for each of the user's top 10 rated anime, the top 150 ALS scores, and the top 40 of each of the user's 3 most frequent genres among their liked anime. Every candidate is then scored on all three signals: content is its best cosine similarity to one of the user's top rated anime, which also gives `similar_to` its `anime_id`. Onboarding `likedGenres` aren't stored anywhere the engine can read yet, so liked genres come from ratings only.

### Blending

```
n       = number of rated or completed anime for the user
w_cf    = min(1, n / 20) × 0.6            (0 when CF is disabled)
w_pop   = 0.1
w_cont  = 1 − w_cf − w_pop
score   = w_cont × content + w_cf × cf + w_pop × popularity
```

Each component is normalized to 0–1 within the candidate set before blending.

### Filters

Applied after blending, in order:

1. Remove anything in `user_anime` (any status) or `user_feedback` with `not_interested`.
2. Remove adult titles unless the user has `show_adult`.
3. Sequel ordering: if a candidate has an unwatched prequel, replace it with the earliest unwatched entry in that chain (following `PREQUEL` relations; the replacement keeps the candidate's score and reason). If the chain runs into an entry that can't be shown (planned, dropped, not interested, or adult for this user), the candidate is dropped rather than shown out of order.
4. Diversity: at most 3 items per primary genre in any window of 10. The catalog doesn't record genre order, so an anime's primary genre is its rarest one (fewest titles in the catalog).

### Reasons

Each item gets the reason for its largest blended component: `similar_to` (with the rated anime whose neighbor it was), `liked_by_similar_users`, or `popular_in_genre`. Users with no data get `popular`, as does a popularity-led item outside the user's liked genres.

If schema `app` doesn't exist yet (the API hasn't migrated), every user is treated as unknown and `train` finds no data; nothing fails.

## Testing

- Unit tests cover blending weights and normalization, reason selection, exclusions, sequel ordering, diversity, the confidence table, and the ALS user step. The user-step test checks against implicit's own `recalculate_user`. All use small in-memory fixtures.
- Integration tests create a fresh database on the Postgres at `REC_TEST_POSTGRES_URL`. The role needs `CREATE DATABASE`, and pgvector must be installed. Setup:
  - It enables pgvector, loads the catalog contract (`services/ingest-worker/contract/catalog-schema.sql`) and a stand-in for the `app` schema (`tests/sql/app_schema.sql`, written from `infra/README.md`), then applies this service's migrations.
  - Swap the stand-in for the API's Flyway migrations once they exist.
  - The catalog is synthetic: 60 anime in 6 genre clusters, a sequel chain, and one adult and one removed title, plus 80 synthetic users.
  - A deterministic bag-of-words embedder stands in for the real model, so tests never download one.
- They cover:
  - migrations and incremental embedding
  - similar (itself and relations excluded, adult hidden, 404)
  - recommendations for unknown users, content-only users, sequel ordering, planned prequels, adult opt-in, and diversity
  - training thresholds and model activation, CF for users who joined after training, and evaluation against a popularity baseline
  - the nightly advisory lock, and a missing `app` schema
- Not done yet: Testcontainers, the real embedding model in tests (it can't be downloaded in the build sandbox), and a latency check at catalog scale.
- `rec evaluate` reports recall@20 and nDCG@20 on a random 20% hold-out of ratings ≥ 8, next to a popularity-only baseline. Record the numbers in the change description when touching ranking logic.

Evaluation on the synthetic test data (60 users, 96 held-out ratings) when this was written: recall@20 0.954 and nDCG@20 0.586, against 0.383 and 0.156 for popularity alone (both arms exclude what the user already has). The clusters make this far easier than real data. Re-run on real ratings once there are users.

## Rules

- Owns schema `rec` and nothing else in Postgres. Reads `catalog.*` and `app.*` with schema-qualified names and never writes them; the `rec_svc` role has only `SELECT` there.
- Never returns display text. Anything a person reads is produced by the API.
- Never calls external services at request time. Model downloads happen at image build time.
- Request latency target: p95 under 300 ms for `limit=20`. The API gives up at 800 ms.
- Changing the reason code set or response shape updates the API README in the same change.
