"""Batch embedding: only anime whose text changed since the last run are re-embedded."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cache
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from rec_engine.config import settings
from rec_engine.content import text as content_text
from rec_engine.db import Database

log = logging.getLogger(__name__)
BATCH = 64


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        """Unit-length vectors, one row per text."""
        ...


class SentenceTransformerEmbedder:
    """The production embedder. The model is baked into the image at build time, so
    nothing is downloaded at runtime."""

    def __init__(self, name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.name = name
        self.model = SentenceTransformer(name, device="cpu")
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> NDArray[np.float32]:
        vectors = self.model.encode(
            texts, batch_size=BATCH, normalize_embeddings=True, show_progress_bar=False
        )
        return np.asarray(vectors, dtype=np.float32)


@cache
def default_embedder() -> Embedder:
    return SentenceTransformerEmbedder(settings().embedding_model)


@dataclass
class EmbedResult:
    total: int
    embedded: int
    unchanged: int
    removed: int


def run(db: Database, embedder: Embedder | None = None, force: bool = False) -> EmbedResult:
    embedder = embedder or default_embedder()
    if embedder.dim != settings().embedding_dim:
        raise ValueError(
            f"{embedder.name} makes {embedder.dim}-dim vectors; the rec schema stores "
            f"{settings().embedding_dim}. A new migration is needed first."
        )
    items = db.embedding_inputs()
    known = {} if force else db.embedding_hashes(embedder.name)
    todo = []
    for item in items:
        body = content_text.build(item)
        digest = content_text.source_hash(body, embedder.name)
        if known.get(item.anime_id) != digest:
            todo.append((item.anime_id, body, digest))
    for i in range(0, len(todo), BATCH * 8):
        chunk = todo[i : i + BATCH * 8]
        vectors = embedder.encode([body for _, body, _ in chunk])
        db.upsert_embeddings(
            embedder.name, [(a, vectors[j], h) for j, (a, _, h) in enumerate(chunk)]
        )
        log.info("embedded %d/%d", i + len(chunk), len(todo))
    removed = db.delete_embeddings_except(embedder.name, (item.anime_id for item in items))
    return EmbedResult(len(items), len(todo), len(items) - len(todo), removed)
