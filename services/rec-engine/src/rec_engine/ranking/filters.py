"""Filters applied after blending, in order:

1. Anything in the user's list (any status) or marked not_interested.
2. Adult titles unless the user opted in.
3. Sequel ordering: a candidate with an unwatched prequel is replaced by the earliest
   unwatched entry of its chain. If that chain passes through something the user
   can't be shown (planned, dropped, not interested, adult), the candidate is dropped.
4. Diversity: at most 3 items per primary genre in any window of 10.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

WATCHED = ("completed", "watching")
DIVERSITY_WINDOW = 10
DIVERSITY_MAX = 3
MAX_CHAIN = 20


def exclude(
    ids: Sequence[int], excluded: set[int], adult: Mapping[int, bool], show_adult: bool
) -> list[int]:
    return [a for a in ids if a not in excluded and (show_adult or not adult.get(a, False))]


def earliest_unwatched(
    anime_id: int,
    prequels: Mapping[int, Sequence[int]],
    watched: set[int],
    showable: Callable[[int], bool],
) -> int | None:
    """Walk PREQUEL links back from `anime_id`. Returns the entry to show instead, or
    None when the chain can't be shown."""
    current, seen = anime_id, {anime_id}
    for _ in range(MAX_CHAIN):
        options = [p for p in prequels.get(current, ()) if p not in seen]
        if not options:
            return current
        prequel = min(options)  # more than one prequel is rare; lowest id is the oldest entry
        if prequel in watched:
            return current
        if not showable(prequel):
            return None
        seen.add(prequel)
        current = prequel
    return current


def sequel_order(
    ranked: Sequence[int],
    prequels: Mapping[int, Sequence[int]],
    watched: set[int],
    showable: Callable[[int], bool],
) -> list[tuple[int, int]]:
    """Returns (shown id, ranked id it replaces), deduplicated, best first."""
    out: list[tuple[int, int]] = []
    seen: set[int] = set()
    for anime_id in ranked:
        shown = earliest_unwatched(anime_id, prequels, watched, showable)
        if shown is None or shown in seen:
            continue
        seen.add(shown)
        out.append((shown, anime_id))
    return out


def diversify[T](items: Sequence[T], primary: Callable[[T], str | None], limit: int) -> list[T]:
    """Greedy: take the best item whose primary genre has fewer than 3 among the last 9
    picked; if none qualifies, take the best remaining."""
    pending = list(items)
    out: list[T] = []
    while pending and len(out) < limit:
        window = [primary(x) for x in out[-(DIVERSITY_WINDOW - 1) :]]
        for i, item in enumerate(pending):
            genre = primary(item)
            if genre is None or window.count(genre) < DIVERSITY_MAX:
                out.append(pending.pop(i))
                break
        else:
            out.append(pending.pop(0))
    return out


def primary_genre(genres: Sequence[str], genre_sizes: Mapping[str, int]) -> str | None:
    """The anime's most specific genre (fewest titles in the catalog). Genre order isn't
    meaningful in the source data, so the rarest one stands in for "primary"."""
    if not genres:
        return None
    return min(genres, key=lambda g: (genre_sizes.get(g, 0), g))
