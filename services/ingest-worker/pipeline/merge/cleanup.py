"""Text cleanup for source descriptions."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

MIN_TEXT_LENGTH = 20

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SOURCE_CREDIT = re.compile(r"\n*\s*\((?:source|written by)\b[^)]*\)\s*$", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n\s*\n+")
_SPACES = re.compile(r"[ \t ]+")

# [spoiler]…[/spoiler] and [spoiler=label]…[/spoiler] are dropped with their content.
_SHIKI_SPOILER = re.compile(r"\[spoiler(?:=[^\]]*)?\].*?\[/spoiler\]", re.IGNORECASE | re.DOTALL)
# [character=123]Name[/character], [anime=1]X[/anime], [url=…]x[/url], [b]x[/b], ...
_SHIKI_TAG = re.compile(r"\[/?[a-z_]+(?:=[^\]]*)?\]", re.IGNORECASE)


def _normalize(text: str) -> str | None:
    lines = [_SPACES.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    text = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    return text if len(text) >= MIN_TEXT_LENGTH else None


def anilist_description(raw: str | None) -> str | None:
    """`<br>` → newline, strip other tags, drop a trailing "(Source: …)" credit."""
    if not raw:
        return None
    html = _BR.sub("\n", raw)
    text = HTMLParser(f"<div>{html}</div>").text(separator="")
    text = text.strip()
    while True:
        stripped = _SOURCE_CREDIT.sub("", text).rstrip()
        if stripped == text:
            break
        text = stripped
    return _normalize(text)


def shikimori_description(raw: str | None) -> str | None:
    """Drop [spoiler] blocks, replace every other BBCode tag with its inner text."""
    if not raw:
        return None
    text = _SHIKI_SPOILER.sub("", raw)
    text = _SHIKI_TAG.sub("", text)
    return _normalize(text)


def clean_title(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = _SPACES.sub(" ", raw).strip()
    return text or None
