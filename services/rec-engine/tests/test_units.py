"""Blending, filters, sequel ordering, reasons, confidences, and the ALS user solve,
with small in-memory fixtures."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.sparse import csr_matrix

from rec_engine.cf.als import AlsModel
from rec_engine.cf.matrix import build, confidence, user_confidences
from rec_engine.db import Interaction, UserData
from rec_engine.ranking import blend, filters, reasons
from rec_engine.ranking.candidates import Candidate, CandidateSet, liked_genres


@pytest.mark.parametrize(
    ("n", "cf_enabled", "expected"),
    [
        (0, True, (0.9, 0.0, 0.1)),
        (10, True, (0.6, 0.3, 0.1)),
        (20, True, (0.3, 0.6, 0.1)),
        (100, True, (0.3, 0.6, 0.1)),
        (100, False, (0.9, 0.0, 0.1)),
    ],
)
def test_weights(n, cf_enabled, expected):
    w = blend.weights(n, cf_enabled)
    assert (round(w.content, 6), round(w.cf, 6), round(w.popularity, 6)) == expected


def test_normalize_and_blend():
    assert blend.normalize([2.0, 4.0, 3.0]) == [0.0, 1.0, 0.5]
    assert blend.normalize([5.0, 5.0]) == [0.0, 0.0]
    items = [
        Candidate(1, content=0.9, content_from=7, cf=0.0, popularity=1.0),
        Candidate(2, content=0.1, cf=5.0, popularity=2.0),
        Candidate(3, content=0.1, cf=0.0, popularity=9.0),
    ]
    ranked = blend.blend(items, blend.Weights(0.3, 0.6, 0.1))
    # content [1, 0, 0], cf [0, 1, 0], popularity [0, 1/8, 1] after normalization
    assert [c.anime_id for c in ranked] == [2, 1, 3]
    assert [c.reason_component for c in ranked] == ["cf", "content", "popularity"]
    assert ranked[0].score == pytest.approx(0.6 + 0.1 * (1 / 8))


SIMILAR = {"code": "similar_to", "anime_id": 9}
CF = {"code": "liked_by_similar_users"}
POPULAR = {"code": "popular"}


def in_genre(genre: str) -> dict[str, str]:
    return {"code": "popular_in_genre", "genre": genre}


@pytest.mark.parametrize(
    ("candidate", "genres", "liked", "expected"),
    [
        (Candidate(1, content_from=9, reason_component="content"), [], [], SIMILAR),
        (Candidate(1, reason_component="cf"), [], [], CF),
        (Candidate(1, via_genre="action", reason_component="popularity"), ["drama"], [], in_genre("action")),
        (Candidate(1, reason_component="popularity"), ["drama", "romance"], ["romance"], in_genre("romance")),
        (Candidate(1, reason_component="popularity"), ["drama"], ["romance"], POPULAR),
        # Content won, but there's no liked anime to point at.
        (Candidate(1, reason_component="content"), ["drama"], [], POPULAR),
    ],
)  # fmt: skip
def test_reasons(candidate, genres, liked, expected):
    assert reasons.reason(candidate, genres, liked) == expected


def test_exclusions_and_adult():
    adult = {3: True}
    assert filters.exclude([1, 2, 3, 4], {2}, adult, show_adult=False) == [1, 4]
    assert filters.exclude([1, 2, 3, 4], {2}, adult, show_adult=True) == [1, 3, 4]


PREQUELS = {3: [2], 2: [1], 1: [], 12: [11], 11: [10]}


@pytest.mark.parametrize(
    ("anime", "watched", "blocked", "expected"),
    [
        (3, set(), set(), 1),  # nothing watched: start from the beginning
        (3, {1}, set(), 2),  # watched S1: show S2
        (3, {2}, set(), 3),  # watched S2: S3 is next
        (3, set(), {1}, None),  # S1 can't be shown (e.g. planned): drop the sequel
        (1, set(), set(), 1),  # no prequel
    ],
)
def test_earliest_unwatched(anime, watched, blocked, expected):
    assert (
        filters.earliest_unwatched(anime, PREQUELS, watched, lambda a: a not in blocked) == expected
    )


def test_sequel_order_replaces_and_deduplicates():
    ordered = filters.sequel_order([3, 2, 12, 5], PREQUELS, watched={10}, showable=lambda a: True)
    assert ordered == [(1, 3), (11, 12), (5, 5)]  # 2 also maps to 1, so it's dropped


def test_sequel_chain_cycle_terminates():
    cyclic = {1: [2], 2: [1]}
    assert filters.earliest_unwatched(1, cyclic, set(), lambda a: True) == 2


def test_diversity_at_most_three_per_genre_in_any_ten():
    items = [(g, i) for g in "abcd" for i in range(6)]  # ranked: all a, then all b, ...
    out = filters.diversify(items, lambda x: x[0], limit=20)
    genres = [g for g, _ in out]
    assert len(genres) == 20
    for start in range(len(genres)):
        window = genres[start : start + 10]
        assert all(window.count(g) <= 3 for g in "abcd"), genres
    assert genres[:4] == ["a", "a", "a", "b"]


def test_diversity_relaxes_when_nothing_fits():
    out = filters.diversify([("a", i) for i in range(6)], lambda x: x[0], limit=5)
    assert len(out) == 5


def test_primary_genre_is_the_rarest():
    sizes = {"action": 900, "mecha": 40, "drama": 700}
    assert filters.primary_genre(["action", "drama", "mecha"], sizes) == "mecha"
    assert filters.primary_genre([], sizes) is None


def test_candidate_set_caps_and_excludes():
    pool = CandidateSet(excluded={1})
    assert pool.add(1) is None
    for i in range(2, 600):
        pool.add(i)
    assert len(pool) == 500
    assert liked_genres([["a", "b"], ["b"], ["c", "b", "a"]]) == ["b", "a", "c"]


@pytest.mark.parametrize(
    ("status", "score", "expected"),
    [
        ("completed", 10, 5.0),
        ("completed", 8, 3.4),
        ("completed", 6, 1.8),
        ("completed", 5, None),
        ("watching", 3, None),
        ("completed", None, 2.0),
        ("watching", None, 1.5),
        ("planned", None, 0.5),
        ("dropped", 9, None),
    ],
)
def test_confidence_table(status, score, expected):
    assert confidence(status, score) == (pytest.approx(expected) if expected else None)


def test_matrix_skips_blocked_and_dropped():
    m = build(
        [
            Interaction("u1", 10, "completed", 9),
            Interaction("u1", 11, "dropped", None),
            Interaction("u1", 12, "planned", None),
            Interaction("u2", 10, "watching", None),
        ],
        blocked={("u1", 12)},
    )
    assert m.users == ["u1", "u2"] and m.items == [10]
    assert m.values.toarray().tolist() == [[pytest.approx(4.2)], [1.5]]
    assert user_confidences({1: ("completed", 7), 2: ("planned", None)}, not_interested=[2]) == {
        1: pytest.approx(2.6)
    }


def test_user_vector_matches_implicit_recalculate_user():
    from implicit.cpu.als import AlternatingLeastSquares

    rng = np.random.default_rng(0)
    dense = (rng.random((40, 30)) > 0.8) * rng.integers(1, 5, (40, 30))
    user_items = csr_matrix(dense.astype(np.float32))
    reference = AlternatingLeastSquares(factors=8, iterations=5, regularization=0.1, random_state=1)
    reference.fit(user_items, show_progress=False)
    model = AlsModel("t", np.arange(30), np.asarray(reference.item_factors), 0.1, 40)

    row = user_items[3]
    ours = model.user_vector({int(i): float(v) for i, v in zip(row.indices, row.data, strict=True)})
    theirs = reference.recalculate_user(3, row)
    assert np.allclose(ours, np.asarray(theirs).ravel(), atol=1e-3)


def test_user_data_helpers():
    user = UserData(
        "u",
        anime={
            1: ("completed", 9),
            2: ("completed", None),
            3: ("completed", 4),
            4: ("planned", None),
        },
        not_interested={5},
    )
    assert user.excluded == {1, 2, 3, 4, 5}
    assert user.rated_or_completed == 3
    assert user.top_rated() == [1, 2]
