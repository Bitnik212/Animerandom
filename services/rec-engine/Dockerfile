FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf \
    OPENBLAS_NUM_THREADS=1

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The embedding model: CPU-only torch (the PyPI build bundles CUDA), then the model
# itself, downloaded now so nothing is fetched at runtime.
ARG EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2
RUN uv pip install --python /opt/venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch \
 && uv pip install --python /opt/venv/bin/python "sentence-transformers>=3.3,<6" \
 && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

COPY . .
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10002 rec && mkdir -p /data/models && chown -R rec /data /app
USER rec

EXPOSE 8081
CMD ["uvicorn", "rec_engine.app:app", "--host", "0.0.0.0", "--port", "8081"]
