from django.db import models
from django.db.models import F, Q


class Rank(models.TextChoices):
    PRIVATE = "Private"
    SERGEANT = "Sergeant"
    LIEUTENANT = "Lieutenant"
    GENERAL = "General"


class TaskStatus(models.TextChoices):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


ACTIVE_STATUSES = [TaskStatus.PENDING, TaskStatus.RUNNING]


class TaskStatusQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status__in=ACTIVE_STATUSES)


class Person(models.Model):
    """A civilian registered at peacetime (<host>/people), later drafted (<host>/draft/<n>)."""

    external_id = models.PositiveIntegerField(unique=True, help_text="The 'id' from the API.")
    name = models.CharField(max_length=100)  # not unique in the source data
    age = models.PositiveSmallIntegerField()
    key = models.CharField(max_length=100, blank=True)  # only some people carry one
    raw = models.JSONField(help_text="Original JSON object from /people.")

    # Filled in by drafting. `health` is the current health, lowered by fight simulations.
    rank = models.CharField(max_length=20, choices=Rank.choices, blank=True)
    health = models.PositiveSmallIntegerField(null=True, blank=True)
    draft_raw = models.JSONField(null=True, blank=True, help_text="JSON object from /draft/<n>.")
    drafted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["external_id"]
        verbose_name_plural = "people"

    def __str__(self):
        return f"{self.name} (#{self.external_id})"

    @property
    def drafted_health(self):
        """Health at drafting time; simulations start from this value."""
        return (self.draft_raw or {}).get("health")


class BatchLog(models.Model):
    """Status and response log of one background task that downloads <endpoint>/<number>."""

    Status = TaskStatus

    number = models.PositiveIntegerField(unique=True)
    status = models.CharField(max_length=10, choices=TaskStatus.choices, default=TaskStatus.PENDING)
    task_id = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    item_count = models.PositiveSmallIntegerField(default=0)
    next_batch = models.PositiveIntegerField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)  # last progress, for stale detection

    objects = TaskStatusQuerySet.as_manager()

    class Meta:
        abstract = True
        ordering = ["number"]

    def __str__(self):
        return f"Batch {self.number} ({self.status})"

    @property
    def duration_seconds(self):
        return duration_seconds(self.started_at, self.finished_at)


class DraftBatch(BatchLog):
    """One /draft/<number> download. item_count = soldiers drafted."""

    class Meta(BatchLog.Meta):
        verbose_name_plural = "draft batches"


class FightBatch(BatchLog):
    """One /fight/<number> download. item_count = fights stored."""

    class Meta(BatchLog.Meta):
        verbose_name_plural = "fight batches"


class FightStatus(models.TextChoices):
    NEW = "new", "Not simulated"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Fight(models.Model):
    """A fight between two soldiers from /fight/<n>, plus the result of its last simulation."""

    Status = FightStatus

    external_id = models.PositiveIntegerField(unique=True, help_text="The 'fight_id' from the API.")
    batch_number = models.PositiveIntegerField()
    blue = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="blue_fights")
    red = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="red_fights")
    raw = models.JSONField(help_text="Original JSON object from /fight/<n>.")

    # Simulation state and results (reset at the start of every simulation).
    status = models.CharField(max_length=10, choices=FightStatus.choices, default=FightStatus.NEW)
    task_id = models.CharField(max_length=64, blank=True)
    blue_damage_taken = models.PositiveIntegerField(default=0)
    red_damage_taken = models.PositiveIntegerField(default=0)
    winner = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, blank=True, related_name="won_fights"
    )  # null after a finished fight means a draw
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TaskStatusQuerySet.as_manager()

    class Meta:
        ordering = ["external_id"]
        constraints = [
            models.CheckConstraint(condition=~Q(blue=F("red")), name="fight_between_two_soldiers"),
        ]

    def __str__(self):
        return f"Fight {self.external_id}: {self.blue.name} vs {self.red.name}"

    @property
    def duration_seconds(self):
        return duration_seconds(self.started_at, self.finished_at)


class FightEvent(models.Model):
    """One step of a fight: `attacker` hits `defender` for `damage` health points."""

    fight = models.ForeignKey(Fight, on_delete=models.CASCADE, related_name="events")
    order = models.PositiveSmallIntegerField(help_text="Position in the fight's steps, from 0.")
    attacker = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="attacks")
    defender = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="defenses")
    damage = models.PositiveSmallIntegerField()

    # Set when the event is replayed by a simulation.
    defender_health_after = models.PositiveSmallIntegerField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["fight", "order"]
        constraints = [
            models.UniqueConstraint(fields=["fight", "order"], name="unique_event_order_per_fight"),
            models.CheckConstraint(condition=~Q(attacker=F("defender")), name="event_attacker_is_not_defender"),
        ]

    def __str__(self):
        return f"Fight {self.fight_id} step {self.order}: {self.attacker_id} -> {self.defender_id} ({self.damage})"


def duration_seconds(started_at, finished_at):
    if started_at and finished_at:
        return round((finished_at - started_at).total_seconds(), 1)
    return None
