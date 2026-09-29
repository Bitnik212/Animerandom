"""ID mapping tables (schema `ingest`)."""

from django.db import models


class IdSourceEntry(models.Model):
    """One row of an external cross-reference file (arm or the manami snapshot)."""

    source = models.TextField()  # arm | manami
    anilist_id = models.IntegerField(null=True, blank=True)
    mal_id = models.IntegerField(null=True, blank=True)
    annict_id = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["source", "anilist_id"]),
            models.Index(fields=["source", "mal_id"]),
        ]


class IdMapping(models.Model):
    """Resolved cross-source ids per AniList anime, produced by the map_ids stage."""

    anilist_id = models.IntegerField(primary_key=True)
    mal_id = models.IntegerField(null=True, blank=True)
    mal_via = models.TextField(blank=True)  # anilist | manami | arm | vector | override
    shikimori_id = models.TextField(null=True, blank=True)  # set after a successful fetch
    shikimori_via = models.TextField(blank=True)  # override: fetch this id instead of mal_id
    annict_id = models.IntegerField(null=True, blank=True)
    annict_via = models.TextField(blank=True)  # arm | vector | override
    # Vector matching (pipeline/idmap/vector.py): score of an accepted match, and when a
    # search last ran, so an anime nobody can match isn't searched on every run.
    mal_score = models.FloatField(null=True, blank=True)
    annict_score = models.FloatField(null=True, blank=True)
    shikimori_searched_at = models.DateTimeField(null=True, blank=True)
    annict_searched_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
