"""Field precedence: first non-empty candidate wins; an override always comes first.

One function per row of the precedence table in the service README. Every
decision is recorded in `MergedAnime.choices`, which is what the merge-preview
endpoint shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pipeline import vocab
from pipeline.merge import cleanup
from pipeline.sources import anilist, annict, shikimori

MIN_TAG_RANK = 60

Candidate = tuple[str, Any]  # (source, value)


@dataclass
class Choice:
    value: Any
    source: str | None
    candidates: list[Candidate]


@dataclass
class LocalizedText:
    title: str | None = None
    title_source: str | None = None
    title_machine: bool = False
    synopsis: str | None = None
    synopsis_source: str | None = None
    synopsis_machine: bool = False


@dataclass
class StudioRef:
    anilist_id: int
    name: str
    is_main: bool
    is_animation_studio: bool


@dataclass
class TagRef:
    slug: str
    rank: int
    is_spoiler: bool
    general_spoiler: bool
    category: str | None


@dataclass
class MergedAnime:
    anilist_id: int
    fields: dict[str, Any] = field(default_factory=dict)
    localizations: dict[str, LocalizedText] = field(default_factory=dict)
    synonyms: list[tuple[str, str | None]] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    tags: list[TagRef] = field(default_factory=list)
    studios: list[StudioRef] = field(default_factory=list)
    relations: list[tuple[int, str]] = field(default_factory=list)  # (related AniList id, kind)
    choices: dict[str, Choice] = field(default_factory=dict)
    unknown_genres: list[str] = field(default_factory=list)
    unknown_tags: list[str] = field(default_factory=list)


@dataclass
class Override:
    field: str
    locale: str | None
    value: str


@dataclass
class MergeInput:
    anilist: anilist.Media
    shikimori: shikimori.Anime | None = None
    annict: annict.Work | None = None
    mal_id: int | None = None
    shikimori_id: str | None = None
    annict_id: int | None = None
    overrides: list[Override] = field(default_factory=list)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and not value)


def _override(inp: MergeInput, name: str, locale: str | None = None) -> Override | None:
    for o in inp.overrides:
        if o.field == name and o.locale == locale:
            return o
    return None


def pick(
    merged: MergedAnime,
    key: str,
    candidates: list[Candidate],
    override: Override | None = None,
    coerce: Any = None,
) -> Any:
    if override is not None:
        value = (coerce(override.value) if coerce else override.value) if override.value else None
        merged.choices[key] = Choice(value, "override", candidates)
        return value
    for source, value in candidates:
        if not _empty(value):
            merged.choices[key] = Choice(value, source, candidates)
            return value
    merged.choices[key] = Choice(None, None, candidates)
    return None


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _score(media: anilist.Media) -> list[Candidate]:
    def tenth(v: int | None) -> Decimal | None:
        return None if v is None else (Decimal(v) / 10).quantize(Decimal("0.1"), ROUND_HALF_UP)

    return [
        ("anilist.averageScore", tenth(media.averageScore)),
        ("anilist.meanScore", tenth(media.meanScore)),
    ]


_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿]")


def guess_locale(text: str) -> str | None:
    if _CYRILLIC.search(text):
        return "ru"
    if _JAPANESE.search(text):
        return "ja"
    return None


def merge(inp: MergeInput) -> MergedAnime:
    a = inp.anilist
    s = inp.shikimori
    n = inp.annict
    m = MergedAnime(anilist_id=a.id)
    loc = {locale: LocalizedText() for locale in ("en", "ru", "ja", "ja_latn")}

    # --- Titles -------------------------------------------------------------
    title_candidates: dict[str, list[Candidate]] = {
        "en": [("anilist", cleanup.clean_title(a.title.english))],
        "ru": [
            ("shikimori.license_name_ru", cleanup.clean_title(s.license_name_ru) if s else None),
            ("shikimori.russian", cleanup.clean_title(s.russian) if s else None),
        ],
        "ja": [
            ("anilist", cleanup.clean_title(a.title.native)),
            ("annict", cleanup.clean_title(n.title) if n else None),
        ],
        "ja_latn": [("anilist", cleanup.clean_title(a.title.romaji))],
    }
    for locale, candidates in title_candidates.items():
        value = pick(m, f"title.{locale}", candidates, _override(inp, "title", locale))
        loc[locale].title = value
        loc[locale].title_source = _source_name(m.choices[f"title.{locale}"].source)

    # --- Synopses -----------------------------------------------------------
    synopsis_candidates: dict[str, list[Candidate]] = {
        "en": [("anilist", cleanup.anilist_description(a.description))],
        "ru": [("shikimori", cleanup.shikimori_description(s.description) if s else None)],
        "ja": [],
    }
    for locale, candidates in synopsis_candidates.items():
        value = pick(m, f"synopsis.{locale}", candidates, _override(inp, "synopsis", locale))
        loc[locale].synopsis = value
        loc[locale].synopsis_source = _source_name(m.choices[f"synopsis.{locale}"].source)
    m.localizations = loc

    # --- Scalar fields ------------------------------------------------------
    f = m.fields
    f["anilist_id"] = a.id
    f["mal_id"] = pick(m, "mal_id", [("idmap", inp.mal_id)], _override(inp, "mal_id"), int)
    f["shikimori_id"] = pick(
        m, "shikimori_id", [("shikimori", inp.shikimori_id)], _override(inp, "shikimori_id")
    )
    f["annict_id"] = pick(
        m,
        "annict_id",
        [("idmap", str(inp.annict_id) if inp.annict_id else None)],
        _override(inp, "annict_id"),
    )
    f["score"] = pick(
        m,
        "score",
        _score(a),
        _override(inp, "score"),
        lambda v: Decimal(v).quantize(Decimal("0.1")),
    )
    for name, value, coerce in (
        ("format", a.format, None),
        ("status", a.status, None),
        ("season", a.season, None),
        ("season_year", a.seasonYear, int),
        ("episodes", a.episodes, int),
        ("duration_min", a.duration, int),
        ("cover_url", a.coverImage.extraLarge or a.coverImage.large, None),
    ):
        f[name] = pick(m, name, [("anilist", value)], _override(inp, name), coerce)
    f["is_adult"] = bool(
        pick(m, "is_adult", [("anilist", a.isAdult)], _override(inp, "is_adult"), _bool)
    )
    f["popularity"] = a.popularity

    # --- Synonyms -----------------------------------------------------------
    main_titles = {t.casefold() for t in (x.title for x in loc.values()) if t}
    raw_synonyms: list[str | None] = list(a.synonyms)
    if s:
        raw_synonyms += [
            *s.synonyms,
            *s.english,
            *s.japanese,
            s.russian if s.license_name_ru else None,
        ]
    if n:
        raw_synonyms += [n.titleEn, n.title, n.titleRo]
    raw_synonyms += [a.title.english, a.title.native]
    seen: set[str] = set()
    for value in raw_synonyms:
        text = cleanup.clean_title(value)
        if not text or text.casefold() in seen or text.casefold() in main_titles:
            continue
        seen.add(text.casefold())
        m.synonyms.append((text, guess_locale(text)))

    # --- Vocabulary, studios, relations ------------------------------------
    for name in a.genres:
        slug = vocab.genres().slug_for("anilist", name)
        if slug is None:
            m.unknown_genres.append(name)
        elif slug not in m.genres:
            m.genres.append(slug)
    for t in a.tags:
        if (t.rank or 0) < MIN_TAG_RANK:
            continue
        slug = vocab.tags().slug_for("anilist", t.name)
        if slug is None:
            m.unknown_tags.append(t.name)
            continue
        m.tags.append(
            TagRef(
                slug=slug,
                rank=t.rank or 0,
                is_spoiler=t.isMediaSpoiler or t.isGeneralSpoiler,
                general_spoiler=t.isGeneralSpoiler,
                category=t.category,
            )
        )
    m.studios = [
        StudioRef(e.node.id, e.node.name, e.isMain, e.node.isAnimationStudio)
        for e in a.studios.edges
    ]
    m.relations = [
        (e.node.id, e.relationType or "OTHER")
        for e in a.relations.edges
        if e.node.type in (None, "ANIME") and e.node.id != a.id
    ]
    return m


def _source_name(choice_source: str | None) -> str | None:
    """'shikimori.russian' → 'shikimori'; stored as title_source / synopsis_source."""
    return choice_source.split(".", 1)[0] if choice_source else None
