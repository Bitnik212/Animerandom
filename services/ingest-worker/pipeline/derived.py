"""Derived filter values shared by the Elasticsearch documents and the Redis pools."""

from __future__ import annotations

SCORE_POOLS = (6, 7, 8, 9)


def decade(season_year: int | None) -> str | None:
    return f"{season_year // 10 * 10}s" if season_year else None


def length_bucket(episodes: int | None) -> str | None:
    """short ≤ 13 episodes, medium 14–26, long > 26."""
    if not episodes:
        return None
    if episodes <= 13:
        return "short"
    return "medium" if episodes <= 26 else "long"
