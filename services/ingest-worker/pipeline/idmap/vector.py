"""Vector matching: link an AniList anime to a Shikimori or Annict entry when no
cross-reference (AniList idMal, arm, manami) knows the pair.

Each candidate from a title search is compared with the AniList anime as a feature
vector:

    [title similarity, year agreement, format agreement, episode agreement]

Title similarity is the best cosine similarity between character n-gram vectors of
any title pair (so romaji matches romaji and Japanese matches Japanese), lowered when
the titles carry different numbers ("Season 2" vs "Season 3"). The final score is the
weighted sum. A match is accepted only when it is both good and clearly better than
the runner-up; near misses are reported for review instead of guessed.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from django.conf import settings

WEIGHTS = (0.65, 0.15, 0.10, 0.10)  # title, year, format, episodes
NUMBER_MISMATCH_PENALTY = 0.8
UNKNOWN = 0.5  # agreement score when either side lacks the value

# Canonical formats across sources.
FORMATS = {
    # AniList
    "TV": "tv", "TV_SHORT": "tv", "MOVIE": "movie", "OVA": "ova", "ONA": "ona",
    "SPECIAL": "special", "MUSIC": "music",
    # Shikimori kinds
    "tv": "tv", "tv_13": "tv", "tv_24": "tv", "tv_48": "tv", "movie": "movie", "ova": "ova",
    "ona": "ona", "special": "special", "tv_special": "special", "music": "music",
    "pv": "special", "cm": "special",
    # Annict media
    "WEB": "ona", "OTHER": "special",
}  # fmt: skip

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class Entry:
    """One side of a comparison, normalized from any source."""

    id: str
    titles: tuple[str, ...]
    year: int | None = None
    format: str | None = None
    episodes: int | None = None


@dataclass
class Scored:
    entry: Entry
    score: float
    features: tuple[float, float, float, float]


@dataclass
class Match:
    decision: str  # accepted | review | none
    best: Scored | None
    ranked: list[Scored] = field(default_factory=list)

    @property
    def accepted(self) -> Entry | None:
        return self.best.entry if self.decision == "accepted" and self.best else None

    def describe(self, limit: int = 3) -> str:
        return "; ".join(
            f"{s.entry.id} {s.entry.titles[0] if s.entry.titles else '?'!r} "
            f"score={s.score:.2f} features={tuple(round(f, 2) for f in s.features)}"
            for s in self.ranked[:limit]
        )


def normalize(title: str) -> str:
    text = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(_NON_WORD.sub(" ", text).split())


@cache
def ngrams(title: str) -> Counter[str]:
    """Character bigrams and trigrams of the normalized title, with word boundaries."""
    text = f" {normalize(title)} "
    grams: Counter[str] = Counter()
    for n in (2, 3):
        grams.update(text[i : i + n] for i in range(len(text) - n + 1))
    return grams


def cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def _numbers(title: str) -> frozenset[str]:
    return frozenset(_NUMBER.findall(normalize(title)))


def title_similarity(a: Iterable[str], b: Iterable[str]) -> float:
    best = 0.0
    b_list = [t for t in b if t and normalize(t)]
    for ta in a:
        if not ta or not normalize(ta):
            continue
        for tb in b_list:
            sim = cosine(ngrams(ta), ngrams(tb))
            if _numbers(ta) != _numbers(tb):
                sim *= NUMBER_MISMATCH_PENALTY
            best = max(best, sim)
    return best


def _year_score(a: int | None, b: int | None) -> float:
    if a is None or b is None:
        return UNKNOWN
    return {0: 1.0, 1: 0.5}.get(abs(a - b), 0.0)


def _format_score(a: str | None, b: str | None) -> float:
    fa, fb = FORMATS.get(a or ""), FORMATS.get(b or "")
    if fa is None or fb is None:
        return UNKNOWN
    return 1.0 if fa == fb else 0.0


def _episodes_score(a: int | None, b: int | None) -> float:
    if not a or not b:
        return UNKNOWN
    return max(0.0, 1.0 - abs(a - b) / max(a, b))


def features(target: Entry, candidate: Entry) -> tuple[float, float, float, float]:
    return (
        title_similarity(target.titles, candidate.titles),
        _year_score(target.year, candidate.year),
        _format_score(target.format, candidate.format),
        _episodes_score(target.episodes, candidate.episodes),
    )


def score(vector: Sequence[float]) -> float:
    return sum(w * f for w, f in zip(WEIGHTS, vector, strict=True))


def match(target: Entry, candidates: Iterable[Entry]) -> Match:
    ranked = sorted(
        (Scored(c, score(f), f) for c in candidates for f in [features(target, c)]),
        key=lambda s: s.score,
        reverse=True,
    )
    if not ranked:
        return Match("none", None)
    best = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if (
        best.score >= settings.VECTOR_MATCH_ACCEPT
        and best.score - runner_up >= settings.VECTOR_MATCH_MARGIN
    ):
        return Match("accepted", best, ranked)
    if best.score >= settings.VECTOR_MATCH_REVIEW:
        return Match("review", best, ranked)
    return Match("none", best, ranked)


# --- Source adapters ---------------------------------------------------------------


def from_anilist(media: dict[str, Any]) -> Entry:
    t = media.get("title") or {}
    titles = [t.get("romaji"), t.get("english"), t.get("native"), *(media.get("synonyms") or [])]
    if not t.get("romaji") and t.get("native"):
        # Only a Japanese title: add a generated romaji so Latin-script candidates compare.
        from pipeline.localize.romaji import romanize

        if generated := romanize(t["native"]):
            titles.append(generated[0])
    return Entry(
        id=str(media["id"]),
        titles=tuple(x for x in titles if x),
        year=media.get("seasonYear"),
        format=media.get("format"),
        episodes=media.get("episodes"),
    )


def from_shikimori(item: dict[str, Any]) -> Entry:
    titles = [item.get("name"), item.get("russian"), *(item.get("english") or [])]
    titles += item.get("japanese") or []
    aired = item.get("aired_on") or ""
    return Entry(
        id=str(item["id"]),
        titles=tuple(x for x in titles if x),
        year=int(aired[:4]) if aired[:4].isdigit() else None,
        format=item.get("kind"),
        episodes=item.get("episodes") or None,
    )


def from_annict(work: dict[str, Any]) -> Entry:
    titles = [work.get("title"), work.get("titleRo"), work.get("titleEn"), work.get("titleKana")]
    return Entry(
        id=str(work["annictId"]),
        titles=tuple(x for x in titles if x),
        year=work.get("seasonYear"),
        format=work.get("media"),
        episodes=work.get("episodesCount") or None,
    )


def search_text(media: dict[str, Any], prefer_native: bool = False) -> str | None:
    """The title to search a source with: Japanese for Annict, romaji otherwise."""
    t = media.get("title") or {}
    order = ("native", "romaji", "english") if prefer_native else ("romaji", "english", "native")
    return next((t[k] for k in order if t.get(k)), None)
