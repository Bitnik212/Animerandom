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
    mal_via = models.TextField(blank=True)  # anilist | manami
    shikimori_id = models.TextField(null=True, blank=True)  # set after a successful fetch
    annict_id = models.IntegerField(null=True, blank=True)
    annict_via = models.TextField(blank=True)  # arm
    updated_at = models.DateTimeField(auto_now=True)
