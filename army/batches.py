"""Downloading batched data (/draft/<n>, /fight/<n>): shared bookkeeping for batch tasks."""

import logging
import random
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import drafting, fights
from .api import ChallengeAPIError, fetch_batch
from .models import DraftBatch, FightBatch, TaskStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchKind:
    name: str  # also the URL path: <host>/<name>/<n>
    model: type
    save: Callable[[int, list], int]  # (batch number, items) -> items stored
    estimate: Callable[[], int]  # expected number of batches


KINDS = {
    "draft": BatchKind("draft", DraftBatch, drafting.save_soldiers, drafting.estimated_batch_count),
    "fight": BatchKind("fight", FightBatch, fights.save_fights, fights.estimated_batch_count),
}

RESET_FIELDS = {
    "status": TaskStatus.PENDING,
    "task_id": "",
    "attempts": 0,
    "item_count": 0,
    "next_batch": None,
    "response": None,
    "error": "",
    "started_at": None,
    "finished_at": None,
}


class CannotStart(Exception):
    """A run was refused, e.g. because the same work is still in progress."""


def reserve_batches(kind, batch_numbers=None):
    """Reset the rows for the batches about to run and return their numbers.

    batch_numbers=None means a full run: all old rows are replaced. Raises CannotStart if a
    requested batch is still active, so a double submit does not hit the slow server twice.
    """
    full_run = batch_numbers is None
    with transaction.atomic():
        mark_interrupted(kind.model)
        numbers = list(range(kind.estimate())) if full_run else sorted(batch_numbers)
        busy = kind.model.objects.active()
        if not full_run:
            busy = busy.filter(number__in=numbers)
        if busy_numbers := list(busy.values_list("number", flat=True)):
            raise CannotStart(f"{kind.name.title()} batches {busy_numbers} are still running.")
        if full_run:
            kind.model.objects.all().delete()
        for number in numbers:
            kind.model.objects.update_or_create(number=number, defaults=RESET_FIELDS)
    return numbers


def mark_interrupted(model):
    """Fail active rows that made no progress for TASK_STALE_AFTER seconds.

    This happens when the process running them stopped (e.g. a server restart with the
    in-memory thread backend). Works for any model with status/error/finished_at/updated_at.
    """
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.TASK_STALE_AFTER)
    return model.objects.active().filter(updated_at__lt=cutoff).update(
        status=TaskStatus.FAILED,
        error=f"Interrupted: no progress for {settings.TASK_STALE_AFTER}s. Run it again.",
        finished_at=now,
        updated_at=now,
    )


def run_batch(kind, number):
    """Download one batch, store it, and log the outcome on its row. Returns the payload."""
    update_batch(kind, number, status=TaskStatus.RUNNING, started_at=timezone.now())
    try:
        payload = fetch_with_retries(kind, number)
        count = kind.save(number, payload["data"])
    except Exception as exc:
        logger.error("%s batch %s failed: %s", kind.name, number, exc)
        update_batch(kind, number, status=TaskStatus.FAILED, error=str(exc), finished_at=timezone.now())
        raise

    update_batch(
        kind,
        number,
        status=TaskStatus.DONE,
        item_count=count,
        next_batch=payload.get("next_batch"),
        response=payload,
        finished_at=timezone.now(),
    )
    logger.info("%s batch %s done: %d items, next_batch=%s", kind.name, number, count, payload.get("next_batch"))
    return payload


def fetch_with_retries(kind, number):
    for attempt in range(1, settings.BATCH_MAX_ATTEMPTS + 1):
        update_batch(kind, number, attempts=attempt)
        try:
            return fetch_batch(kind.name, number)
        except ChallengeAPIError as exc:
            if not exc.retryable or attempt == settings.BATCH_MAX_ATTEMPTS:
                raise
            delay = settings.BATCH_RETRY_DELAY * 2 ** (attempt - 1) + random.uniform(0, 1)  # jitter
            logger.warning("%s batch %s attempt %d failed (%s); retrying in %.1fs", kind.name, number, attempt, exc, delay)
            time.sleep(delay)


def update_batch(kind, number, **fields):
    # QuerySet.update() skips auto_now, so set updated_at (used for stale detection) here.
    kind.model.objects.filter(number=number).update(updated_at=timezone.now(), **fields)
