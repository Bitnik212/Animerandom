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
| Embeddings | sentence-transformers, model `paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions) |
| Collaborative filtering | `implicit` (ALS) |
| Numerics | numpy, scipy sparse |
| CLI | typer |
| Tests | pytest, Testcontainers for Postgres |
| Lint and types | ruff, mypy (strict) |

The embedding model is multilingual, so a Russian or Japanese query vector can be compared with vectors built from English text if cross-language features are added later.

## Layout

```
services/rec-engine/
├── pyproject.toml
├── alembic.ini
├── migrations/                  Alembic revisions for schema `rec`
├── src/rec_engine/
│   ├── app.py                 FastAPI app and routes
│   ├── cli.py                 `rec` command: embed, train, evaluate
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
│   │   ├── filters.py         exclusions, sequel ordering
│   │   └── reasons.py         reason code selection
│   └── models.py              response schemas
└── tests/
```

## Running

```bash
# from services/rec-engine
uv sync
uv run alembic upgrade head      # migrate schema `rec` (also runs on server start)
uv run rec embed                 # embed anime that are new or changed
uv run rec train                 # train ALS and activate the new model
uv run rec evaluate              # offline metrics on a held-out split
uv run uvicorn rec_engine.app:app --port 8081 --reload
uv run pytest
uv run ruff check . && uv run mypy src
```

From the repo root, `docker compose run --rm rec-engine rec embed` runs a job and `docker compose up -d rec-engine` starts the server. In the nightly schedule, `rec embed` and `rec train` run right after the ingest worker finishes.

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

Unknown user → `200` with popularity-based items. Unknown fields must be ignored by the API so new reason fields can be added safely.

### `GET /v1/similar/{anime_id}?limit=20`

```json
{ "items": [ { "anime_id": 30276, "score": 0.83 } ] }
```

Unknown anime → `404` with `type: anime-not-found`.

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
| Rated 1–10 | `1 + 4 × (score − 5) / 5`, clipped to ≥ 0; ratings of 5 or below contribute nothing positive |
| Completed, no rating | 2 |
| Watching | 1.5 |
| Planned | 0.5 |
| Dropped, not interested | excluded from the matrix, used only as filters |

`rec train` fits ALS, writes `als-{timestamp}.npz` to `MODEL_DIR`, records it in `rec_model`, and marks it active. The server reloads the active model every 5 minutes. Users created after training get CF scores via ALS's `recalculate_user` from their current interactions.

### Candidate generation

Up to 500 candidates from the union of:

1. Nearest neighbors of the user's top 10 rated anime (content).
2. Top ALS scores (if CF is enabled and the user has ≥ 3 interactions).
3. Most popular anime in the user's liked genres (from onboarding and ratings).

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
3. Sequel ordering: if a candidate has an unwatched prequel, replace it with the earliest unwatched entry in that chain.
4. Diversity: at most 3 items per primary genre in any window of 10.

### Reasons

Each item gets the reason for its largest blended component: `similar_to` (with the rated anime whose neighbor it was), `liked_by_similar_users`, or `popular_in_genre`. Users with no data get `popular`.

## Testing

- Unit tests for blending, filters, sequel ordering, and reason selection with small in-memory fixtures.
- Integration tests run against Postgres with pgvector in Testcontainers. They load the catalog contract (`services/ingest-worker/contract/catalog-schema.sql`) and the API's `app` migrations, then this service's Alembic migrations, and use the same fixture catalog as the API plus synthetic users.
- `rec evaluate` reports recall@20 and nDCG@20 on a random 20% hold-out of ratings ≥ 8. Record the numbers in the change description when touching ranking logic.

## Rules

- Owns schema `rec` and nothing else in Postgres. Reads `catalog.*` and `app.*` with schema-qualified names and never writes them; the `rec_svc` role has only `SELECT` there.
- Never returns display text. Anything a person reads is produced by the API.
- Never calls external services at request time. Model downloads happen at image build time.
- Request latency target: p95 under 300 ms for `limit=20`. The API gives up at 800 ms.
- Changing the reason code set or response shape updates the API README in the same change.
