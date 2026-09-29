"""Resolve MAL and Annict ids for AniList anime.

    AniList id ──(AniList's idMal)──> MAL id ──(same id)──> Shikimori
         └──────────────(arm)──────────────> Annict id

The manami snapshot fills MAL gaps. When sources disagree, AniList's idMal wins
and the conflict is reported to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.models import IdMapping, IdSourceEntry


@dataclass
class MapResult:
    mappings: list[IdMapping] = field(default_factory=list)
    conflicts: list[tuple[int, str]] = field(default_factory=list)


def resolve(anilist_mal: dict[int, int | None]) -> MapResult:
    """`anilist_mal` maps AniList id → AniList's own idMal (or None)."""
    ids = list(anilist_mal)
    arm_by_anilist = {
        e.anilist_id: e for e in IdSourceEntry.objects.filter(source="arm", anilist_id__in=ids)
    }
    manami = dict(
        IdSourceEntry.objects.filter(source="manami", anilist_id__in=ids).values_list(
            "anilist_id", "mal_id"
        )
    )

    # MAL first: AniList's idMal, then the manami snapshot, then arm.
    resolved: dict[int, tuple[int | None, str]] = {}
    result = MapResult()
    for anilist_id, anilist_mal_id in anilist_mal.items():
        arm = arm_by_anilist.get(anilist_id)
        candidates = [
            (anilist_mal_id, "anilist"),
            (manami.get(anilist_id), "manami"),
            (arm.mal_id if arm else None, "arm"),
        ]
        mal_id, via = next(((m, v) for m, v in candidates if m), (None, ""))
        resolved[anilist_id] = (mal_id, via)
        for other, name in candidates[1:]:
            if anilist_mal_id and other and other != anilist_mal_id:
                result.conflicts.append(
                    (anilist_id, f"MAL id: AniList says {anilist_mal_id}, {name} says {other}")
                )

    # Annict: arm by AniList id, else arm by the resolved MAL id.
    mal_ids = [m for m, _ in resolved.values() if m]
    arm_by_mal: dict[int, IdSourceEntry] = {}
    for entry in IdSourceEntry.objects.filter(source="arm", mal_id__in=mal_ids).order_by("id"):
        arm_by_mal.setdefault(entry.mal_id, entry)  # type: ignore[arg-type]

    existing = IdMapping.objects.in_bulk(ids)
    for anilist_id, (mal_id, via) in resolved.items():
        m = existing.get(anilist_id) or IdMapping(anilist_id=anilist_id)
        if mal_id != m.mal_id:
            m.shikimori_id = None  # re-resolved by the next Shikimori fetch
        m.mal_id, m.mal_via = mal_id, via
        annict = arm_by_anilist.get(anilist_id) or (arm_by_mal.get(mal_id) if mal_id else None)
        m.annict_id = annict.annict_id if annict else None
        m.annict_via = "arm" if annict else ""
        result.mappings.append(m)
    return result


def save(result: MapResult) -> None:
    IdMapping.objects.bulk_create(
        result.mappings,
        update_conflicts=True,
        unique_fields=["anilist_id"],
        update_fields=[
            "mal_id",
            "mal_via",
            "shikimori_id",
            "annict_id",
            "annict_via",
            "updated_at",
        ],
        batch_size=1000,
    )
