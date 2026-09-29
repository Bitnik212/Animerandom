"""Write merged anime into schema `catalog`: upserts keyed by anilist_id, one
transaction per batch."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import connections, transaction
from django.db.models import Q

from catalog.models import (
    Anime,
    AnimeGenre,
    AnimeLocalization,
    AnimeRelation,
    AnimeStatus,
    AnimeStudio,
    AnimeSynonym,
    AnimeTag,
    Genre,
    Studio,
    Tag,
)
from pipeline.merge.precedence import MergedAnime

DB = "catalog"
ANIME_UPDATE_FIELDS = [
    "mal_id",
    "shikimori_id",
    "annict_id",
    "format",
    "status",
    "season",
    "season_year",
    "episodes",
    "duration_min",
    "score",
    "popularity",
    "cover_url",
    "is_adult",
    "merge_hash",
    "updated_at",
]


@dataclass
class WriteResult:
    written: list[int] = field(default_factory=list)  # AniList ids
    conflicts: list[tuple[int, str]] = field(default_factory=list)


def _dedupe_mal_ids(batch: list[MergedAnime], result: WriteResult) -> None:
    """mal_id is unique in the catalog. If another anime already holds it, keep the
    existing owner and leave this one without a MAL id."""
    wanted = {m.fields["mal_id"]: m for m in batch if m.fields.get("mal_id")}
    owners = dict(Anime.objects.filter(mal_id__in=list(wanted)).values_list("mal_id", "anilist_id"))
    seen: set[int] = set()
    for m in batch:
        mal_id = m.fields.get("mal_id")
        if not mal_id:
            continue
        owner = owners.get(mal_id)
        if (owner is not None and owner != m.anilist_id) or mal_id in seen:
            result.conflicts.append(
                (m.anilist_id, f"MAL id {mal_id} already belongs to AniList {owner}; not stored")
            )
            m.fields["mal_id"] = None
        else:
            seen.add(mal_id)


def write_batch(batch: list[tuple[MergedAnime, str]]) -> WriteResult:
    """`batch` holds (merged anime, merge hash) pairs."""
    result = WriteResult()
    if not batch:
        return result
    merged = [m for m, _ in batch]
    with transaction.atomic(using=DB):
        _dedupe_mal_ids(merged, result)
        # Free MAL ids that this batch moves away from, before claiming new ones.
        Anime.objects.filter(anilist_id__in=[m.anilist_id for m in merged]).update(mal_id=None)
        rows = [
            Anime(**{k: v for k, v in m.fields.items()}, merge_hash=merge_hash)
            for m, merge_hash in batch
        ]
        saved = Anime.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["anilist_id"],
            update_fields=ANIME_UPDATE_FIELDS,
        )
        ids = {a.anilist_id: a.id for a in saved}
        anime_ids = list(ids.values())

        AnimeLocalization.objects.filter(anime_id__in=anime_ids).delete()
        AnimeLocalization.objects.bulk_create(
            [
                AnimeLocalization(
                    anime_id=ids[m.anilist_id],
                    locale=locale,
                    title=text.title,
                    synopsis=text.synopsis,
                    title_source=text.title_source,
                    synopsis_source=text.synopsis_source,
                    title_machine=text.title_machine,
                    synopsis_machine=text.synopsis_machine,
                )
                for m in merged
                for locale, text in m.localizations.items()
                if text.title or text.synopsis
            ]
        )

        AnimeSynonym.objects.filter(anime_id__in=anime_ids).delete()
        AnimeSynonym.objects.bulk_create(
            [
                AnimeSynonym(anime_id=ids[m.anilist_id], value=value, locale=locale)
                for m in merged
                for value, locale in m.synonyms
            ],
            ignore_conflicts=True,
        )

        genre_ids = dict(Genre.objects.values_list("slug", "id"))
        AnimeGenre.objects.filter(anime_id__in=anime_ids).delete()
        AnimeGenre.objects.bulk_create(
            [
                AnimeGenre(anime_id=ids[m.anilist_id], genre_id=genre_ids[slug])
                for m in merged
                for slug in m.genres
                if slug in genre_ids
            ]
        )

        _write_tags(merged, ids)
        _write_studios(merged, ids)
        _write_relations(merged, ids)
    result.written = [m.anilist_id for m in merged]
    return result


def _write_tags(merged: list[MergedAnime], ids: dict[int, int]) -> None:
    tags = {t.slug: t for t in Tag.objects.all()}
    for m in merged:
        for ref in m.tags:
            tag = tags.get(ref.slug)
            if tag is None:
                continue
            changed = []
            if ref.general_spoiler and not tag.is_spoiler:
                tag.is_spoiler = True
                changed.append("is_spoiler")
            if ref.category and not tag.category:
                tag.category = ref.category
                changed.append("category")
            if changed:
                tag.save(update_fields=changed)
    AnimeTag.objects.filter(anime_id__in=list(ids.values())).delete()
    rows: dict[tuple[int, int], AnimeTag] = {}
    for m in merged:
        for ref in m.tags:
            if ref.slug in tags:
                key = (ids[m.anilist_id], tags[ref.slug].id)
                rows[key] = AnimeTag(
                    anime_id=key[0], tag_id=key[1], rank=ref.rank, is_spoiler=ref.is_spoiler
                )
    AnimeTag.objects.bulk_create(list(rows.values()))


def _write_studios(merged: list[MergedAnime], ids: dict[int, int]) -> None:
    refs = {s.anilist_id: s for m in merged for s in m.studios}
    Studio.objects.bulk_create(
        [
            Studio(anilist_id=s.anilist_id, name=s.name, is_animation_studio=s.is_animation_studio)
            for s in refs.values()
        ],
        update_conflicts=True,
        unique_fields=["anilist_id"],
        update_fields=["name", "is_animation_studio"],
    )
    studio_ids = dict(
        Studio.objects.filter(anilist_id__in=list(refs)).values_list("anilist_id", "id")
    )
    AnimeStudio.objects.filter(anime_id__in=list(ids.values())).delete()
    rows: dict[tuple[int, int], AnimeStudio] = {}
    for m in merged:
        for s in m.studios:
            key = (ids[m.anilist_id], studio_ids[s.anilist_id])
            main = s.is_main or (key in rows and rows[key].is_main)
            rows[key] = AnimeStudio(anime_id=key[0], studio_id=key[1], is_main=main)
    AnimeStudio.objects.bulk_create(list(rows.values()))


def _write_relations(merged: list[MergedAnime], ids: dict[int, int]) -> None:
    """Relations to anime not in the catalog yet are filled later by `link_relations`."""
    targets = {rel_id for m in merged for rel_id, _ in m.relations}
    known = dict(Anime.objects.filter(anilist_id__in=list(targets)).values_list("anilist_id", "id"))
    AnimeRelation.objects.filter(anime_id__in=list(ids.values())).delete()
    rows = {
        (ids[m.anilist_id], known[rel_id], kind): AnimeRelation(
            anime_id=ids[m.anilist_id], related_id=known[rel_id], kind=kind
        )
        for m in merged
        for rel_id, kind in m.relations
        if rel_id in known
    }
    AnimeRelation.objects.bulk_create(list(rows.values()))


def link_relations() -> int:
    """Insert relations whose target arrived after the source anime was written, using
    the stored AniList payloads. Insert-only; stale edges go when their source is rewritten."""
    with connections[DB].cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO anime_relation (anime_id, related_id, kind)
            SELECT DISTINCT a.id, b.id, COALESCE(e->>'relationType', 'OTHER')
            FROM raw_source_record r
            JOIN anime a ON r.source = 'anilist' AND a.anilist_id = r.source_id::int
            CROSS JOIN LATERAL jsonb_array_elements(
                COALESCE(r.payload->'relations'->'edges', '[]'::jsonb)) e
            JOIN anime b ON b.anilist_id = (e->'node'->>'id')::int
            WHERE COALESCE(e->'node'->>'type', 'ANIME') = 'ANIME' AND b.id <> a.id
            ON CONFLICT DO NOTHING
            """
        )
        return cursor.rowcount


def mark_removed(seen_anilist_ids: set[int]) -> list[int]:
    """After a complete full run: anime AniList no longer returns become REMOVED."""
    gone = list(
        Anime.objects.exclude(anilist_id__in=seen_anilist_ids)
        .exclude(status=AnimeStatus.REMOVED)
        .values_list("id", flat=True)
    )
    if gone:
        Anime.objects.filter(id__in=gone).update(status=AnimeStatus.REMOVED)
    return gone


def poolable() -> Q:
    return Q(is_adult=False) & ~Q(status=AnimeStatus.REMOVED)
