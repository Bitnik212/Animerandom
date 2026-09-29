"""Vector matching: features, scores, and accept / review / none decisions."""

import pytest

from pipeline.idmap import vector
from pipeline.idmap.vector import Entry

AOT = Entry("16498", ("Shingeki no Kyojin", "Attack on Titan", "進撃の巨人"), 2013, "TV", 25)
AOT_S2 = Entry("20958", ("Shingeki no Kyojin Season 2", "進撃の巨人 Season2"), 2017, "TV", 12)
S2_CANDIDATE = Entry("x", ("Shingeki no Kyojin Season 2",), 2017, "tv", 12)
TWINS = [Entry("a", AOT.titles, 2013, "TV", 25), Entry("b", AOT.titles, 2013, "TV", 25)]


@pytest.mark.parametrize(
    ("a", "b", "low", "high"),
    [
        ("Shingeki no Kyojin", "shingeki no kyojin", 1.0, 1.0),  # case
        ("Steins;Gate", "STEINS GATE", 1.0, 1.0),  # punctuation
        ("ＳＴＥＩＮＳ；ＧＡＴＥ", "Steins;Gate", 1.0, 1.0),  # full-width (NFKC)
        ("進撃の巨人", "進撃の巨人", 1.0, 1.0),  # Japanese
        ("Kyoukai no Kanata", "Kyokai no Kanata", 0.7, 0.95),  # romanization variant
        ("Shingeki no Kyojin Season 2", "Shingeki no Kyojin Season 3", 0.5, 0.8),  # number penalty
        ("Shingeki no Kyojin", "Cowboy Bebop", 0.0, 0.2),
        ("進撃の巨人", "Shingeki no Kyojin", 0.0, 0.0),  # different scripts never match
    ],
)
def test_title_similarity(a, b, low, high):
    assert low <= vector.title_similarity([a], [b]) <= high


@pytest.mark.parametrize(
    ("target", "candidates", "decision", "winner"),
    [
        # Exact title, same year/format/episodes.
        (AOT, [AOT, AOT_S2], "accepted", "16498"),
        # Sequel: numbers and year/episodes separate it from the first season.
        (AOT_S2, [AOT, S2_CANDIDATE], "accepted", "x"),
        # Japanese titles against Annict-style entries, with Annict's media names.
        (
            Entry("1", ("Steins;Gate", "STEINS;GATE", "シュタインズ・ゲート"), 2011, "TV", 24),
            [Entry("1745", ("シュタインズ・ゲート", "Steins;Gate"), 2011, "TV", 24),
             Entry("9999", ("シュタインズ・ゲート ゼロ",), 2018, "TV", 23)],
            "accepted", "1745",
        ),
        # Two equally good candidates: too close to call, so reported for review.
        (AOT, TWINS, "review", "a"),
        # Same title but wrong year, format and length: review, not a guess.
        (AOT, [Entry("m", ("Shingeki no Kyojin",), 2020, "MOVIE", 1)], "review", "m"),
        # Unrelated.
        (AOT, [Entry("cb", ("Cowboy Bebop",), 1998, "TV", 26)], "none", "cb"),
        # Nothing found.
        (AOT, [], "none", None),
    ],
)  # fmt: skip
def test_match_decisions(target, candidates, decision, winner):
    result = vector.match(target, candidates)
    assert result.decision == decision, result.describe()
    assert (result.best.entry.id if result.best else None) == winner
    assert (result.accepted is not None) == (decision == "accepted")


def test_thresholds_come_from_settings(settings):
    candidate = Entry("m", ("Shingeki no Kyojin",), 2020, "MOVIE", 1)
    assert vector.match(AOT, [candidate]).decision == "review"
    settings.VECTOR_MATCH_ACCEPT = 0.6
    assert vector.match(AOT, [candidate]).decision == "accepted"


def test_formats_are_compared_across_sources():
    assert vector._format_score("TV_SHORT", "tv_13") == 1.0  # AniList vs Shikimori
    assert vector._format_score("ONA", "WEB") == 1.0  # AniList vs Annict
    assert vector._format_score("MOVIE", "tv") == 0.0
    assert vector._format_score(None, "tv") == vector.UNKNOWN


def test_adapters():
    shiki = vector.from_shikimori(
        {"id": 5114, "name": "Hagane no Renkinjutsushi", "russian": "Стальной алхимик",
         "kind": "tv", "episodes": 64, "aired_on": "2009-04-05"}
    )  # fmt: skip
    assert (shiki.id, shiki.year, shiki.format, shiki.episodes) == ("5114", 2009, "tv", 64)
    work = vector.from_annict(
        {"annictId": 1, "title": "鋼の錬金術師", "seasonYear": 2009, "media": "TV"}
    )
    assert (work.titles, work.year) == (("鋼の錬金術師",), 2009)
    only_native = vector.from_anilist({"id": 1, "title": {"native": "ぼくらの日常"}})
    assert "Bokura no nichijou" in only_native.titles  # generated romaji for Latin candidates


def test_search_text_prefers_script_per_source():
    media = {
        "title": {
            "romaji": "Shingeki no Kyojin",
            "english": "Attack on Titan",
            "native": "進撃の巨人",
        }
    }
    assert vector.search_text(media) == "Shingeki no Kyojin"
    assert vector.search_text(media, prefer_native=True) == "進撃の巨人"
    assert vector.search_text({"title": {}}) is None
