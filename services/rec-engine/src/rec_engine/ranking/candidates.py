"""Candidate generation and per-candidate signals."""

from __future__ import annotations

from dataclasses import dataclass

MAX_CANDIDATES = 500
CONTENT_SEEDS = 10  # the user's top rated anime
NEIGHBORS_PER_SEED = 50
CF_CANDIDATES = 150
GENRE_CANDIDATES = 40  # per liked genre
LIKED_GENRES = 3
MIN_CF_INTERACTIONS = 3


@dataclass
class Candidate:
    anime_id: int
    content: float = 0.0  # max cosine similarity to one of the user's liked anime
    content_from: int | None = None  # that liked anime
    cf: float = 0.0  # ALS score
    popularity: float = 0.0  # log popularity
    via_genre: str | None = None  # liked genre it was found through
    score: float = 0.0  # blended
    reason_component: str = "popularity"


class CandidateSet:
    """Union of the three sources, capped at MAX_CANDIDATES in insertion order."""

    def __init__(self, excluded: set[int]) -> None:
        self.excluded = excluded
        self.items: dict[int, Candidate] = {}

    def add(self, anime_id: int) -> Candidate | None:
        if anime_id in self.excluded:
            return None
        if anime_id not in self.items:
            if len(self.items) >= MAX_CANDIDATES:
                return None
            self.items[anime_id] = Candidate(anime_id)
        return self.items[anime_id]

    def __len__(self) -> int:
        return len(self.items)

    def values(self) -> list[Candidate]:
        return list(self.items.values())


def liked_genres(genres_of_liked: list[list[str]], limit: int = LIKED_GENRES) -> list[str]:
    """Most frequent genres among the user's liked anime (ties by name)."""
    counts: dict[str, int] = {}
    for genres in genres_of_liked:
        for g in genres:
            counts[g] = counts.get(g, 0) + 1
    return [g for g, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
