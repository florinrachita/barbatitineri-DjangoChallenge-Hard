"""Background tasks (django.tasks) and the functions that start them.

Downloads: start_download() reserves rows and enqueues download_all, which enqueues one
download_batch task per batch. All batches are enqueued up front so they run in parallel;
each batch's `next_batch` is also followed in case the estimate was too low.

Simulation: start_simulation() resets health and enqueues simulate_all, which enqueues one
simulate_fight task per fight. Fights run in parallel; see fights.apply_event for how
concurrent health updates stay correct.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.tasks import task

from . import fights
from .batches import KINDS, CannotStart, mark_interrupted, reserve_batches, run_batch
from .models import Fight, FightBatch, Person, TaskStatus

logger = logging.getLogger(__name__)


# Downloads ---------------------------------------------------------------------------------

def start_download(kind_name, batch_numbers=None):
    """Download every batch (batch_numbers=None) or only the given ones. Returns the TaskResult."""
    kind = KINDS[kind_name]
    if kind_name == "fight" and not Person.objects.exists():
        raise CannotStart("Register people first (import_people): fights refer to soldiers.")
    numbers = reserve_batches(kind, batch_numbers)
    try:
        return download_all.enqueue(kind_name, numbers, follow_next=batch_numbers is None)
    except Exception as exc:
        kind.model.objects.filter(number__in=numbers).update(status=TaskStatus.FAILED, error=f"Could not enqueue: {exc}")
        raise


@task
def download_all(kind_name, batch_numbers, follow_next=False):
    """Enqueue one download_batch task per batch, so batches download in parallel."""
    for number in batch_numbers:
        enqueue_batch(kind_name, number, follow_next)
    logger.info("Enqueued %s batches %s", kind_name, batch_numbers)
    return batch_numbers


@task
def download_batch(kind_name, number, follow_next=False):
    """Download <host>/<kind>/<number> and store it (soldier ranks, or fights and events)."""
    kind = KINDS[kind_name]
    payload = run_batch(kind, number)
    next_batch = payload.get("next_batch")
    if follow_next and next_batch is not None:
        _, created = kind.model.objects.get_or_create(number=next_batch)
        if created:
            enqueue_batch(kind_name, next_batch, follow_next=True)
    return {"batch": number, "items": len(payload["data"]), "next_batch": next_batch}


def enqueue_batch(kind_name, number, follow_next):
    result = download_batch.enqueue(kind_name, number, follow_next=follow_next)
    KINDS[kind_name].model.objects.filter(number=number).update(task_id=result.id)


# Fight simulation --------------------------------------------------------------------------

def start_simulation():
    """Reset health to drafted values and replay every fight in parallel. Returns the TaskResult."""
    with transaction.atomic():
        mark_interrupted(FightBatch)
        mark_interrupted(Fight)
        if FightBatch.objects.active().exists():
            raise CannotStart("Fight data is still downloading.")
        if Fight.objects.active().exists():
            raise CannotStart("A simulation is already running.")
        if not Fight.objects.exists():
            raise CannotStart("No fights yet: download them first (import_fights).")
        undrafted_fighters = Person.objects.filter(draft_raw=None).filter(
            Q(blue_fights__isnull=False) | Q(red_fights__isnull=False)
        )
        if undrafted_fighters.exists():
            raise CannotStart("Some fighters are not drafted yet: run draft_people first.")
        fights.reset_simulation()
        fight_ids = list(Fight.objects.values_list("external_id", flat=True))
    try:
        return simulate_all.enqueue(fight_ids)
    except Exception as exc:
        Fight.objects.update(status=Fight.Status.FAILED, error=f"Could not enqueue: {exc}")
        raise


@task
def simulate_all(fight_ids):
    """Enqueue one simulate_fight task per fight, so fights run in parallel."""
    for fight_id in fight_ids:
        result = simulate_fight.enqueue(fight_id)
        Fight.objects.filter(external_id=fight_id).update(task_id=result.id)
    logger.info("Enqueued %d fight simulations", len(fight_ids))
    return fight_ids


@task
def simulate_fight(fight_id):
    """Replay one fight: lower the defender's health for each of its events."""
    try:
        return fights.replay_fight(fight_id)
    except Exception as exc:
        logger.error("Fight %s failed: %s", fight_id, exc)
        fights.update_fight(fight_id, status=Fight.Status.FAILED, error=str(exc))
        raise
