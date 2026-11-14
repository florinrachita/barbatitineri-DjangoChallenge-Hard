"""Background drafting.

start_drafting() reserves the DraftBatch rows and enqueues draft_all, which enqueues one
draft_batch task per /draft/<n> batch. All batches are enqueued up front (the count is
estimated from registered people) so they run in parallel; each batch's `next_batch` is
also followed in case the estimate was too low.
"""

import logging
import math
import random
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.tasks import task
from django.utils import timezone

from .api import ChallengeAPIError, fetch_draft_batch
from .models import DraftBatch, Person

logger = logging.getLogger(__name__)

RESET_FIELDS = {
    "status": DraftBatch.Status.PENDING,
    "task_id": "",
    "attempts": 0,
    "drafted_count": 0,
    "next_batch": None,
    "response": None,
    "error": "",
    "started_at": None,
    "finished_at": None,
}


class DraftingInProgress(Exception):
    def __init__(self, numbers):
        super().__init__(f"Batches {numbers} are still being drafted.")
        self.numbers = numbers


def start_drafting(batch_numbers=None):
    """Draft everyone (batch_numbers=None) or re-draft only the given batches.

    Returns the draft_all TaskResult. Raises DraftingInProgress if a requested batch is
    still active, so a double submit does not hit the slow server twice.
    """
    full_run = batch_numbers is None
    with transaction.atomic():
        mark_interrupted_batches()
        numbers = list(range(estimated_batch_count())) if full_run else sorted(batch_numbers)
        busy = DraftBatch.objects.active()
        if not full_run:
            busy = busy.filter(number__in=numbers)
        if busy_numbers := list(busy.values_list("number", flat=True)):
            raise DraftingInProgress(busy_numbers)
        if full_run:
            DraftBatch.objects.all().delete()
        for number in numbers:
            DraftBatch.objects.update_or_create(number=number, defaults=RESET_FIELDS)

    try:
        return draft_all.enqueue(numbers, follow_next=full_run)
    except Exception as exc:
        DraftBatch.objects.filter(number__in=numbers).update(
            status=DraftBatch.Status.FAILED, error=f"Could not enqueue: {exc}", updated_at=timezone.now()
        )
        raise


def mark_interrupted_batches():
    """Fail active batches that made no progress for DRAFT_STALE_AFTER seconds.

    This happens when the process running them stopped (e.g. a server restart with the
    in-memory thread backend). Returns how many were marked.
    """
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.DRAFT_STALE_AFTER)
    return DraftBatch.objects.active().filter(updated_at__lt=cutoff).update(
        status=DraftBatch.Status.FAILED,
        error=f"Interrupted: no progress for {settings.DRAFT_STALE_AFTER}s. Draft this batch again.",
        finished_at=now,
        updated_at=now,
    )


@task
def draft_all(batch_numbers, follow_next=False):
    """Enqueue one draft_batch task per batch, so batches download in parallel."""
    for number in batch_numbers:
        enqueue_batch(number, follow_next)
    logger.info("Enqueued draft batches %s", batch_numbers)
    return batch_numbers


@task
def draft_batch(number, follow_next=False):
    """Download /draft/<number>, store each soldier's rank, and log the response."""
    update_batch(number, status=DraftBatch.Status.RUNNING, started_at=timezone.now())
    try:
        payload = fetch_with_retries(number)
        drafted = save_soldiers(payload["data"])
    except Exception as exc:
        logger.error("Draft batch %s failed: %s", number, exc)
        update_batch(number, status=DraftBatch.Status.FAILED, error=str(exc), finished_at=timezone.now())
        raise

    next_batch = payload.get("next_batch")
    update_batch(
        number,
        status=DraftBatch.Status.DONE,
        drafted_count=drafted,
        next_batch=next_batch,
        response=payload,
        finished_at=timezone.now(),
    )
    logger.info("Draft batch %s done: %d soldiers, next_batch=%s", number, drafted, next_batch)

    if follow_next and next_batch is not None:
        _, created = DraftBatch.objects.get_or_create(number=next_batch)
        if created:
            enqueue_batch(next_batch, follow_next=True)
    return {"batch": number, "drafted": drafted, "next_batch": next_batch}


def estimated_batch_count():
    return max(1, math.ceil(Person.objects.count() / settings.DRAFT_BATCH_SIZE))


def enqueue_batch(number, follow_next):
    result = draft_batch.enqueue(number, follow_next=follow_next)
    DraftBatch.objects.filter(number=number).update(task_id=result.id)


def update_batch(number, **fields):
    # QuerySet.update() skips auto_now, so set updated_at (used for stale detection) here.
    DraftBatch.objects.filter(number=number).update(updated_at=timezone.now(), **fields)


def fetch_with_retries(number):
    for attempt in range(1, settings.DRAFT_MAX_ATTEMPTS + 1):
        update_batch(number, attempts=attempt)
        try:
            return fetch_draft_batch(number)
        except ChallengeAPIError as exc:
            if not exc.retryable or attempt == settings.DRAFT_MAX_ATTEMPTS:
                raise
            delay = settings.DRAFT_RETRY_DELAY * 2 ** (attempt - 1) + random.uniform(0, 1)  # jitter
            logger.warning("Batch %s attempt %d failed (%s); retrying in %.1fs", number, attempt, exc, delay)
            time.sleep(delay)


@transaction.atomic
def save_soldiers(items):
    now = timezone.now()
    for item in items:
        fields = {
            "name": item["name"],
            "age": item["age"],
            "key": item.get("key", ""),
            "rank": item.get("rank", ""),
            "health": item.get("health"),
            "draft_raw": item,
            "drafted_at": now,
        }
        Person.objects.update_or_create(
            external_id=item["id"],
            defaults=fields,
            create_defaults={**fields, "raw": item},  # someone missing from /people
        )
    return len(items)
