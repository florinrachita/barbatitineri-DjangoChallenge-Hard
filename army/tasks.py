"""Background drafting tasks.

draft_all schedules one draft_batch task per /draft/<n> batch. Batches are enqueued
up front (batch count estimated from registered people) so they run in parallel;
each batch's `next_batch` is also followed in case the estimate was too low.
"""

import logging
import math
import time

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


@task
def draft_all(batch_numbers=None):
    """Draft everyone (batch_numbers=None) or re-draft only the given batches."""
    follow_next = batch_numbers is None
    if follow_next:
        DraftBatch.objects.all().delete()
        batch_numbers = list(range(estimated_batch_count()))

    # Create every row before enqueueing, so follow-up scheduling never duplicates a batch.
    for number in batch_numbers:
        DraftBatch.objects.update_or_create(number=number, defaults=RESET_FIELDS)
    for number in batch_numbers:
        enqueue_batch(number, follow_next)
    logger.info("Scheduled draft batches %s", batch_numbers)
    return batch_numbers


@task
def draft_batch(number, follow_next=False):
    """Download /draft/<number>, store each soldier's rank, and log the response."""
    DraftBatch.objects.filter(number=number).update(
        status=DraftBatch.Status.RUNNING, started_at=timezone.now()
    )
    try:
        payload = fetch_with_retries(number)
        drafted = save_soldiers(payload["data"])
    except Exception as exc:
        logger.error("Draft batch %s failed: %s", number, exc)
        finish_batch(number, DraftBatch.Status.FAILED, error=str(exc))
        raise

    next_batch = payload.get("next_batch")
    finish_batch(
        number,
        DraftBatch.Status.DONE,
        drafted_count=drafted,
        next_batch=next_batch,
        response=payload,
    )
    logger.info("Draft batch %s done: %d soldiers, next_batch=%s", number, drafted, next_batch)

    if follow_next and next_batch is not None and not DraftBatch.objects.filter(number=next_batch).exists():
        DraftBatch.objects.create(number=next_batch)
        enqueue_batch(next_batch, follow_next=True)
    return {"batch": number, "drafted": drafted, "next_batch": next_batch}


def estimated_batch_count():
    return max(1, math.ceil(Person.objects.count() / settings.DRAFT_BATCH_SIZE))


def enqueue_batch(number, follow_next):
    result = draft_batch.enqueue(number, follow_next=follow_next)
    DraftBatch.objects.filter(number=number).update(task_id=result.id)


def fetch_with_retries(number):
    for attempt in range(1, settings.DRAFT_MAX_ATTEMPTS + 1):
        DraftBatch.objects.filter(number=number).update(attempts=attempt)
        try:
            return fetch_draft_batch(number)
        except ChallengeAPIError as exc:
            if not exc.retryable or attempt == settings.DRAFT_MAX_ATTEMPTS:
                raise
            delay = 2**attempt
            logger.warning("Batch %s attempt %d failed (%s); retrying in %ds", number, attempt, exc, delay)
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


def finish_batch(number, status, **fields):
    DraftBatch.objects.filter(number=number).update(status=status, finished_at=timezone.now(), **fields)
