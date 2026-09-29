"""One case per row of the precedence table, plus overrides."""

from decimal import Decimal

import pytest

from pipeline.localize.fallback import localize
from pipeline.merge.precedence import MergeInput, Override, merge
from pipeline.sources import anilist, annict, shikimori
from tests.conftest import fixture

MEDIA = {m["id"]: m for m in fixture("anilist_page.json")["data"]["Page"]["media"]}


def media(anilist_id: int, **changes) -> anilist.Media:
    return anilist.Media.model_validate({**MEDIA[anilist_id], **changes})


SHIKI_AOT = shikimori.Anime.model_validate(fixture("shikimori_16498.json"))
SHIKI_SG = shikimori.Anime.model_validate(fixture("shikimori_9253.json"))
ANNICT_SG = annict.Work.model_validate(
    fixture("annict_works.json")["data"]["searchWorks"]["nodes"][0]
)


def run(inp: MergeInput):
    return localize(merge(inp))


@pytest.mark.parametrize(
    ("inp", "key", "value", "source"),
    [
        # title.en: AniList english
        (MergeInput(media(16498)), "title.en", "Attack on Titan", "anilist"),
        (MergeInput(media(21827)), "title.en", None, None),
        # title.ru: Shikimori license_name_ru, then russian
        (
            MergeInput(media(16498), shikimori=SHIKI_AOT),
            "title.ru",
            "Атака на титанов",
            "shikimori.license_name_ru",
        ),
        (
            MergeInput(media(9253), shikimori=SHIKI_SG),
            "title.ru",
            "Врата Штейна",
            "shikimori.russian",
        ),
        (MergeInput(media(9253)), "title.ru", None, None),
        # title.ja: AniList native, then Annict title
        (MergeInput(media(9253), annict=ANNICT_SG), "title.ja", "STEINS;GATE", "anilist"),
        (
            MergeInput(
                media(9253, title={"romaji": "Steins;Gate", "native": None}), annict=ANNICT_SG
            ),
            "title.ja",
            "シュタインズ・ゲート",
            "annict",
        ),
        # title.ja_latn: AniList romaji, then generated
        (MergeInput(media(16498)), "title.ja_latn", "Shingeki no Kyojin", "anilist"),
        (MergeInput(media(100001)), "title.ja_latn", "Bokura no nichijou", "cutlet"),
        # synopsis.en: AniList description (cleaned)
        (
            MergeInput(media(9253)),
            "synopsis.en",
            "Eccentric scientist Rintarou Okabe has a never-ending thirst for scientific "
            "exploration.",
            "anilist",
        ),
        (MergeInput(media(21827)), "synopsis.en", None, None),  # "Short." is noise
        # synopsis.ru: Shikimori description (cleaned)
        (
            MergeInput(media(16498), shikimori=SHIKI_AOT),
            "synopsis.ru",
            "Много лет назад человечество было почти уничтожено титанами.\n\n"
            "Люди живут за стенами.",
            "shikimori",
        ),
        # synopsis.ja: no open source
        (MergeInput(media(16498), shikimori=SHIKI_AOT), "synopsis.ja", None, None),
        # score: averageScore / 10, falling back to meanScore
        (MergeInput(media(16498)), "score", Decimal("8.5"), "anilist.averageScore"),
        (MergeInput(media(21827)), "score", Decimal("7.2"), "anilist.meanScore"),
        # format, status, episodes, season, duration, cover, is_adult: AniList
        (MergeInput(media(21827)), "format", "MOVIE", "anilist"),
        (MergeInput(media(100001)), "status", "RELEASING", "anilist"),
        (MergeInput(media(16498)), "episodes", 25, "anilist"),
        (MergeInput(media(16498)), "season", "SPRING", "anilist"),
        (MergeInput(media(21827)), "duration_min", 90, "anilist"),
        (MergeInput(media(100002)), "is_adult", True, "anilist"),
    ],
)
def test_precedence(inp, key, value, source):
    merged = run(inp)
    choice = merged.choices[key]
    assert (choice.value, choice.source) == (value, source)


def test_generated_romaji_is_flagged_as_machine():
    latn = run(MergeInput(media(100001))).localizations["ja_latn"]
    assert latn.title_machine is True
    assert latn.title_source == "cutlet"
    real = run(MergeInput(media(16498))).localizations["ja_latn"]
    assert real.title_machine is False


@pytest.mark.parametrize(
    ("override", "key", "value"),
    [
        (Override("title", "en", "AoT (fixed)"), "title.en", "AoT (fixed)"),
        (Override("title", "ru", "Атака титанов"), "title.ru", "Атака титанов"),
        (
            Override("synopsis", "en", "A corrected synopsis for the anime."),
            "synopsis.en",
            "A corrected synopsis for the anime.",
        ),
        (Override("score", None, "9.1"), "score", Decimal("9.1")),
        (Override("episodes", None, "26"), "episodes", 26),
        (Override("is_adult", None, "true"), "is_adult", True),
        (Override("title", "en", ""), "title.en", None),  # empty override clears
    ],
)
def test_override_beats_every_source(override, key, value):
    merged = run(MergeInput(media(16498), shikimori=SHIKI_AOT, overrides=[override]))
    assert merged.choices[key].value == value
    assert merged.choices[key].source == "override"


def test_override_ja_latn_is_never_regenerated():
    merged = run(MergeInput(media(100001), overrides=[Override("title", "ja_latn", "")]))
    assert merged.localizations["ja_latn"].title is None


def test_synonyms_are_deduplicated_and_exclude_main_titles():
    merged = run(MergeInput(media(16498), shikimori=SHIKI_AOT))
    values = [v for v, _ in merged.synonyms]
    assert values[:3] == ["AoT", "SnK", "Атака титанов"]
    assert "Вторжение гигантов" in values
    lowered = [v.casefold() for v in values]
    assert len(lowered) == len(set(lowered))
    assert "attack on titan" not in lowered and "shingeki no kyojin" not in lowered
    assert dict(merged.synonyms)["Атака титанов"] == "ru"


def test_genres_and_tags_through_vocabulary():
    merged = run(MergeInput(media(16498)))
    assert merged.genres == ["action", "drama", "fantasy", "mystery"]
    slugs = {t.slug: t for t in merged.tags}
    assert set(slugs) == {"military", "survival", "tragedy", "male-protagonist", "shounen"}
    assert slugs["tragedy"].is_spoiler is True  # media spoiler: stored, flagged
    assert "gore" not in slugs  # rank 55 < 60
    assert merged.unknown_tags == ["Kaiju"]


def test_unknown_genre_is_skipped_and_reported():
    merged = run(MergeInput(media(21827)))
    assert merged.genres == ["fantasy", "slice-of-life"]
    assert merged.unknown_genres == ["Some New Genre"]


def test_studios_and_relations_from_anilist():
    merged = run(MergeInput(media(16498)))
    assert [(s.name, s.is_main) for s in merged.studios] == [
        ("Wit Studio", True),
        ("Pony Canyon", False),
    ]
    assert merged.relations == [(20958, "SEQUEL")]  # manga relation dropped
