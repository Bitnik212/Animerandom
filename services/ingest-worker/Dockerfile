# Build context: repository root (the worker reads infra/elasticsearch/anime-index.json).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# pg_dump for `manage.py dump_catalog_contract`
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app/services/ingest-worker
COPY services/ingest-worker/pyproject.toml services/ingest-worker/uv.lock ./
RUN uv sync --frozen --no-dev --extra flower --no-install-project

COPY infra/elasticsearch/anime-index.json /app/infra/elasticsearch/anime-index.json
COPY services/ingest-worker/ ./
RUN DJANGO_SECRET_KEY=build POSTGRES_URL=postgresql://x@localhost/x \
    python manage.py collectstatic --noinput -v0

RUN useradd --system --uid 10001 ingest && chown -R ingest /app
USER ingest

EXPOSE 8082
CMD ["gunicorn", "ingest.asgi:application", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8082", "-w", "2"]

FROM base AS test
USER root
RUN uv sync --frozen --extra flower --no-install-project
USER ingest
CMD ["pytest"]
