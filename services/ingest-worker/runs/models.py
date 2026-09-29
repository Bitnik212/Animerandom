"""Run history (schema `ingest`). Private to the ingest worker."""

from django.db import models


class RunMode(models.TextChoices):
    FULL = "full"
    INCREMENTAL = "incremental"
    REFRESH = "refresh"  # specific AniList ids, all sources
    MERGE = "merge"  # re-merge from raw_source_record, no network
    INDEX = "index"
    POOLS = "pools"


class RunStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINISHED_STATUSES = (RunStatus.DONE, RunStatus.FAILED, RunStatus.CANCELLED)


class IngestRun(models.Model):
    mode = models.TextField(choices=RunMode.choices)
    status = models.TextField(choices=RunStatus.choices, default=RunStatus.PENDING)
    # limit, sources, anilist_ids, rebuild_index, force
    params = models.JSONField(default=dict, blank=True)
    cancel_requested = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["status"])]

    def __str__(self) -> str:
        return f"Run {self.id} ({self.mode}, {self.status})"

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES


class RunStage(models.Model):
    run = models.ForeignKey(IngestRun, on_delete=models.CASCADE, related_name="stages")
    name = models.TextField()
    position = models.PositiveSmallIntegerField()
    status = models.TextField(choices=RunStatus.choices, default=RunStatus.PENDING)
    # processed, changed, skipped, failed, plus stage-specific counters
    counts = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["run", "position"]
        constraints = [
            models.UniqueConstraint(fields=["run", "name"], name="runstage_run_name_unique")
        ]

    def __str__(self) -> str:
        return f"{self.run_id}:{self.name}"


class RunItemError(models.Model):
    """One failed item (or a logged conflict) inside a stage. The run carries on."""

    run = models.ForeignKey(IngestRun, on_delete=models.CASCADE, related_name="item_errors")
    stage = models.TextField()
    source = models.TextField(blank=True)
    item_id = models.TextField(blank=True)
    kind = models.TextField(default="error")  # error | conflict | skipped
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["run", "stage"])]


class RunAnime(models.Model):
    """Which AniList ids a run touches. Stages read this instead of passing id lists
    through the broker."""

    run = models.ForeignKey(IngestRun, on_delete=models.CASCADE, related_name="anime")
    anilist_id = models.IntegerField()
    anilist_changed = models.BooleanField(default=False)
    written = models.BooleanField(default=False)  # merge produced a new catalog row version

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "anilist_id"], name="runanime_unique")
        ]
