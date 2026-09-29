"""Generated romanized titles: cutlet (Hepburn, foreign words in English spelling),
pykakasi as a fallback."""

from __future__ import annotations

import logging
from functools import cache

log = logging.getLogger(__name__)


@cache
def _cutlet():  # type: ignore[no-untyped-def]
    import cutlet

    katsu = cutlet.Cutlet()
    katsu.use_foreign_spelling = True
    return katsu


@cache
def _kakasi():  # type: ignore[no-untyped-def]
    import pykakasi

    return pykakasi.kakasi()


def _prepare(text: str) -> str:
    # The katakana middle dot separates words; cutlet renders it as "/".
    return text.replace("・", " ").replace("･", " ")


def romanize(text: str) -> tuple[str, str] | None:
    """Returns (romaji, generator name), or None when both generators fail."""
    prepared = _prepare(text)
    try:
        value = _cutlet().romaji(prepared).strip()
        if value:
            return value, "cutlet"
    except Exception:  # cutlet raises on some unusual input; fall back
        log.warning("cutlet failed on %r", text, exc_info=True)
    try:
        parts = [item["hepburn"] for item in _kakasi().convert(prepared)]
        value = " ".join(p for p in parts if p.strip()).strip()
        if value:
            return value[:1].upper() + value[1:], "pykakasi"
    except Exception:
        log.warning("pykakasi failed on %r", text, exc_info=True)
    return None
