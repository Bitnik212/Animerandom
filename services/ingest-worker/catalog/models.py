"""Schema `catalog`: the merged anime catalog.

These tables are a contract read by the API and the rec engine (see
contract/catalog-schema.sql). Add columns freely; never rename or drop one in a
single change.
"""

from django.db import models

LOCALES = ("en", "ru", "ja", "ja_latn")
LOCALE_CHOICES = [(code, code) for code in LOCALES]


class AnimeStatus(models.TextChoices):
    FINISHED = "FINISHED"
    RELEASING = "RELEASING"
    NOT_YET_RELEASED = "NOT_YET_RELEASED"
    CANCELLED = "CANCELLED"
    HIATUS = "HIATUS"
    REMOVED = "REMOVED"  # gone from AniList; kept so user history never dangles


class Anime(models.Model):
    id = models.BigAutoField(primary_key=True)
    anilist_id = models.IntegerField(unique=True)
    mal_id = models.IntegerField(unique=True, null=True, blank=True)
    shikimori_id = models.TextField(null=True, blank=True)
    annict_id = models.TextField(null=True, blank=True)
    format = models.TextField(null=True, blank=True)
    status = models.TextField(null=True, blank=True, choices=AnimeStatus.choices)
    season = models.TextField(null=True, blank=True)
    season_year = models.IntegerField(null=True, blank=True)
    episodes = models.IntegerField(null=True, blank=True)
    duration_min = models.IntegerField(null=True, blank=True)
    score = models.DecimalField(max_digits=3, decimal_places=1, null=True, blank=True)
    popularity = models.IntegerField(null=True, blank=True)
    cover_url = models.TextField(null=True, blank=True)
    is_adult = models.BooleanField(default=False)
    # Hash of every merge input (raw payloads, overrides, merge code version).
    # An unchanged hash means the row is already up to date.
    merge_hash = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "anime"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["season_year"]),
            models.Index(fields=["-popularity"], name="anime_popularity_idx"),
        ]

    def __str__(self) -> str:
        return f"#{self.id} (AniList {self.anilist_id})"


class AnimeLocalization(models.Model):
    pk = models.CompositePrimaryKey("anime_id", "locale")
    anime = models.ForeignKey(Anime, on_delete=models.CASCADE, related_name="localizations")
    locale = models.TextField(choices=LOCALE_CHOICES)
    title = models.TextField(null=True, blank=True)
    synopsis = models.TextField(null=True, blank=True)
    title_source = models.TextField(null=True, blank=True)
    synopsis_source = models.TextField(null=True, blank=True)
    title_machine = models.BooleanField(default=False)
    synopsis_machine = models.BooleanField(default=False)

    class Meta:
        db_table = "anime_localization"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(locale__in=LOCALES), name="anime_localization_locale_check"
            )
        ]


class AnimeSynonym(models.Model):
    anime = models.ForeignKey(Anime, on_delete=models.CASCADE, related_name="synonyms")
    value = models.TextField()
    locale = models.TextField(null=True, blank=True, choices=LOCALE_CHOICES)

    class Meta:
        db_table = "anime_synonym"
        constraints = [
            models.UniqueConstraint(fields=["anime", "value"], name="anime_synonym_unique")
        ]


class Genre(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        db_table = "genre"

    def __str__(self) -> str:
        return self.slug


class GenreLocalization(models.Model):
    pk = models.CompositePrimaryKey("genre_id", "locale")
    genre = models.ForeignKey(Genre, on_delete=models.CASCADE, related_name="localizations")
    locale = models.TextField(choices=LOCALE_CHOICES)
    name = models.TextField()

    class Meta:
        db_table = "genre_localization"


class Tag(models.Model):
    slug = models.SlugField(unique=True, max_length=100)
    category = models.TextField(null=True, blank=True)
    is_spoiler = models.BooleanField(default=False)

    class Meta:
        db_table = "tag"

    def __str__(self) -> str:
        return self.slug


class TagLocalization(models.Model):
    pk = models.CompositePrimaryKey("tag_id", "locale")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="localizations")
    locale = models.TextField(choices=LOCALE_CHOICES)
    name = models.TextField()

    class Meta:
        db_table = "tag_localization"


class AnimeGenre(models.Model):
    pk = models.CompositePrimaryKey("anime_id", "genre_id")
    anime = models.ForeignKey(Anime, on_delete=models.CASCADE)
    genre = models.ForeignKey(Genre, on_delete=models.CASCADE)

    class Meta:
        db_table = "anime_genre"


class AnimeTag(models.Model):
    pk = models.CompositePrimaryKey("anime_id", "tag_id")
    anime = models.ForeignKey(Anime, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)
    rank = models.SmallIntegerField()
    # Spoiler for this anime specifically (AniList isMediaSpoiler);
    # tag.is_spoiler is the general flag.
    is_spoiler = models.BooleanField(default=False)

    class Meta:
        db_table = "anime_tag"


class Studio(models.Model):
    name = models.TextField()
    anilist_id = models.IntegerField(unique=True, null=True, blank=True)
    is_animation_studio = models.BooleanField(default=True)

    class Meta:
        db_table = "studio"

    def __str__(self) -> str:
        return self.name


class AnimeStudio(models.Model):
    pk = models.CompositePrimaryKey("anime_id", "studio_id")
    anime = models.ForeignKey(Anime, on_delete=models.CASCADE)
    studio = models.ForeignKey(Studio, on_delete=models.CASCADE)
    is_main = models.BooleanField(default=False)

    class Meta:
        db_table = "anime_studio"


class AnimeRelation(models.Model):
    pk = models.CompositePrimaryKey("anime_id", "related_id", "kind")
    anime = models.ForeignKey(Anime, on_delete=models.CASCADE, related_name="+")
    related = models.ForeignKey(Anime, on_delete=models.CASCADE, related_name="+")
    kind = models.TextField()  # AniList relationType: SEQUEL, PREQUEL, SIDE_STORY, ...

    class Meta:
        db_table = "anime_relation"


class RawSourceRecord(models.Model):
    pk = models.CompositePrimaryKey("source", "source_id")
    source = models.TextField()  # anilist | shikimori | annict
    source_id = models.TextField()
    payload = models.JSONField()
    payload_hash = models.TextField()
    fetched_at = models.DateTimeField()

    class Meta:
        db_table = "raw_source_record"


class AnimeOverride(models.Model):
    """A manual correction. Beats every source during merge."""

    FIELDS = (
        "title",
        "synopsis",
        "format",
        "status",
        "season",
        "season_year",
        "episodes",
        "duration_min",
        "score",
        "cover_url",
        "is_adult",
        "mal_id",
        "shikimori_id",
        "annict_id",
    )
    LOCALIZED_FIELDS = ("title", "synopsis")

    anime = models.ForeignKey(Anime, on_delete=models.CASCADE, related_name="overrides")
    field = models.TextField(choices=[(f, f) for f in FIELDS])
    locale = models.TextField(null=True, blank=True, choices=LOCALE_CHOICES)
    value = models.TextField(blank=True, help_text="Empty value clears the field.")
    note = models.TextField(blank=True)
    updated_by = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "anime_override"
        constraints = [
            models.UniqueConstraint(
                fields=["anime", "field", "locale"],
                name="anime_override_unique",
                nulls_distinct=False,
            )
        ]

    def __str__(self) -> str:
        suffix = f".{self.locale}" if self.locale else ""
        return f"{self.anime_id}: {self.field}{suffix}"
