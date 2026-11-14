from django.db import models


class Rank(models.TextChoices):
    PRIVATE = "Private"
    SERGEANT = "Sergeant"
    LIEUTENANT = "Lieutenant"
    GENERAL = "General"


class Person(models.Model):
    """A civilian registered at peacetime (<host>/people), later drafted (<host>/draft/<n>)."""

    external_id = models.PositiveIntegerField(unique=True, help_text="The 'id' from the API.")
    name = models.CharField(max_length=100)  # not unique in the source data
    age = models.PositiveSmallIntegerField()
    key = models.CharField(max_length=100, blank=True)  # only some people carry one
    raw = models.JSONField(help_text="Original JSON object from /people.")

    # Filled in by drafting.
    rank = models.CharField(max_length=20, choices=Rank.choices, blank=True)
    health = models.PositiveSmallIntegerField(null=True, blank=True)
    draft_raw = models.JSONField(null=True, blank=True, help_text="JSON object from /draft/<n>.")
    drafted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["external_id"]
        verbose_name_plural = "people"

    def __str__(self):
        return f"{self.name} (#{self.external_id})"


class DraftBatchQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status__in=[DraftBatch.Status.PENDING, DraftBatch.Status.RUNNING])


class DraftBatch(models.Model):
    """Status and response log of one /draft/<number> background task."""

    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        DONE = "done"
        FAILED = "failed"

    number = models.PositiveIntegerField(unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    task_id = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    drafted_count = models.PositiveSmallIntegerField(default=0)
    next_batch = models.PositiveIntegerField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)  # last progress, for stale detection

    objects = DraftBatchQuerySet.as_manager()

    class Meta:
        ordering = ["number"]
        verbose_name_plural = "draft batches"

    def __str__(self):
        return f"Batch {self.number} ({self.status})"

    @property
    def duration_seconds(self):
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 1)
        return None
