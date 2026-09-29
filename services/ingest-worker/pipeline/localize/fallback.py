"""Locale handling at ingest time.

Fallback between languages happens at read time in the API; storage keeps only
real or explicitly generated text. The one generated value is `title.ja_latn`.
The chains below mirror the root README and are used for display in the admin.
"""

from __future__ import annotations

from pipeline.localize import romaji
from pipeline.merge.precedence import MergedAnime

TITLE_CHAIN = {
    "en": ("en", "ja_latn", "ja"),
    "ru": ("ru", "en", "ja_latn", "ja"),
    "ja": ("ja", "ja_latn", "en"),
    "ja_latn": ("ja_latn", "en", "ja"),
}


def display_title(titles: dict[str, str | None], locale: str = "en") -> str | None:
    for candidate in TITLE_CHAIN[locale]:
        if titles.get(candidate):
            return titles[candidate]
    return None


def localize(merged: MergedAnime) -> MergedAnime:
    """Fill a missing `title.ja_latn` from `title.ja`. Overrides are never touched."""
    latn = merged.localizations["ja_latn"]
    ja = merged.localizations["ja"].title
    if latn.title or not ja or merged.choices["title.ja_latn"].source == "override":
        return merged
    generated = romaji.romanize(ja)
    if generated:
        latn.title, latn.title_source = generated
        latn.title_machine = True
        merged.choices["title.ja_latn"].value = latn.title
        merged.choices["title.ja_latn"].source = latn.title_source
    return merged
