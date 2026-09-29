"""Reason codes. The API turns them into text; keep in sync with services/api/README.md.

| Code                    | Extra field | When                                           |
|-------------------------|-------------|------------------------------------------------|
| similar_to              | anime_id    | content was the largest blended component      |
| liked_by_similar_users  | –           | collaborative filtering was the largest        |
| popular_in_genre        | genre       | popularity was the largest, in a liked genre   |
| popular                 | –           | user without data, or no liked genre applies   |
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rec_engine.ranking.candidates import Candidate


def reason(c: Candidate, genres: Sequence[str], liked: Sequence[str]) -> dict[str, Any]:
    if c.reason_component == "content" and c.content_from is not None:
        return {"code": "similar_to", "anime_id": c.content_from}
    if c.reason_component == "cf":
        return {"code": "liked_by_similar_users"}
    genre = c.via_genre or next((g for g in liked if g in genres), None)
    if genre:
        return {"code": "popular_in_genre", "genre": genre}
    return {"code": "popular"}
