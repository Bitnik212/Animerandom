"""The English text block each anime is embedded from:

    {title.en or title.ja_latn}. Genres: {genres}. Themes: {top tags by rank, no spoilers}.
    Studio: {main studio}. {synopsis.en}

Empty parts are left out. English is the most complete synopsis language, which keeps
vectors comparable across the catalog.
"""

from __future__ import annotations

import hashlib

from rec_engine.db import EmbeddingInput


def build(item: EmbeddingInput) -> str:
    parts = []
    if title := item.title_en or item.title_latn:
        parts.append(f"{title}.")
    if item.genres:
        parts.append(f"Genres: {', '.join(item.genres)}.")
    if item.tags:
        parts.append(f"Themes: {', '.join(item.tags)}.")
    if item.studio:
        parts.append(f"Studio: {item.studio}.")
    if item.synopsis_en:
        parts.append(item.synopsis_en.strip())
    return " ".join(parts)


def source_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()
