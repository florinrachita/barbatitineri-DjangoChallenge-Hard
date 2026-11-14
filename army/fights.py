"""Fights: storing /fight/<n> data and replaying fights against soldiers' health."""

from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import Fight, FightEvent, Person

SIMULATION_RESET = {
    "status": Fight.Status.PENDING,
    "task_id": "",
    "blue_damage_taken": 0,
    "red_damage_taken": 0,
    "winner": None,
    "error": "",
    "started_at": None,
    "finished_at": None,
}


class InvalidFightData(ValueError):
    pass


def estimated_batch_count():
    return settings.FIGHT_BATCH_COUNT


@transaction.atomic
def save_fights(batch_number, items):
    """Store fights and their events. Re-downloading a fight replaces its events."""
    soldier_ids = {sid for item in items for sid in (item["blue_soldier_id"], item["red_soldier_id"])}
    soldiers = Person.objects.in_bulk(soldier_ids, field_name="external_id")
    if missing := soldier_ids - soldiers.keys():
        raise InvalidFightData(f"Unknown soldier ids {sorted(missing)}. Run import_people first.")

    for item in items:
        validate_steps(item)
        fight, _ = Fight.objects.update_or_create(
            external_id=item["fight_id"],
            defaults={
                "batch_number": batch_number,
                "blue": soldiers[item["blue_soldier_id"]],
                "red": soldiers[item["red_soldier_id"]],
                "raw": item,
                **SIMULATION_RESET,
                "status": Fight.Status.NEW,
            },
        )
        fight.events.all().delete()
        FightEvent.objects.bulk_create(
            FightEvent(
                fight=fight,
                order=order,
                attacker=soldiers[step["attacker"]],
                defender=soldiers[step["defender"]],
                damage=step["damage"],
            )
            for order, step in enumerate(item["steps"])
        )
    return len(items)


def validate_steps(item):
    fighters = {item["blue_soldier_id"], item["red_soldier_id"]}
    if len(fighters) != 2:
        raise InvalidFightData(f"Fight {item['fight_id']}: a soldier cannot fight themselves.")
    for step in item["steps"]:
        if {step["attacker"], step["defender"]} != fighters:
            raise InvalidFightData(f"Fight {item['fight_id']}: step {step} is not between its two soldiers.")
        if not isinstance(step["damage"], int) or step["damage"] < 0:
            raise InvalidFightData(f"Fight {item['fight_id']}: invalid damage in step {step}.")


def reset_simulation():
    """Restore every soldier's drafted health and clear previous fight results."""
    now = timezone.now()
    soldiers = list(Person.objects.exclude(draft_raw=None))
    for soldier in soldiers:
        soldier.health = soldier.drafted_health
    Person.objects.bulk_update(soldiers, ["health"])
    FightEvent.objects.update(defender_health_after=None, applied_at=None)
    Fight.objects.update(**SIMULATION_RESET, updated_at=now)


def replay_fight(fight_id):
    """Apply each event of the fight, in order, to the defender's health."""
    fight = Fight.objects.get(external_id=fight_id)
    update_fight(fight_id, status=Fight.Status.RUNNING, started_at=timezone.now())

    damage_taken = {fight.blue_id: 0, fight.red_id: 0}
    for event in fight.events.all():
        apply_event(event)
        damage_taken[event.defender_id] += event.damage

    winner_id = fight_winner_id(damage_taken)
    update_fight(
        fight_id,
        status=Fight.Status.DONE,
        blue_damage_taken=damage_taken[fight.blue_id],
        red_damage_taken=damage_taken[fight.red_id],
        winner_id=winner_id,
        finished_at=timezone.now(),
    )
    return {"fight": fight_id, "damage_taken": list(damage_taken.values()), "winner": winner_id}


def apply_event(event):
    """Lower the defender's health by the event's damage (never below 0).

    Fights run in parallel and a soldier can be in several fights, so the subtraction
    happens inside the database (UPDATE ... SET health = MAX(health - damage, 0)).
    Reading health in Python, subtracting, and saving would lose concurrent updates.
    """
    with transaction.atomic():
        Person.objects.filter(pk=event.defender_id).update(health=Greatest(F("health") - event.damage, 0))
        health = Person.objects.values_list("health", flat=True).get(pk=event.defender_id)
        FightEvent.objects.filter(pk=event.pk).update(defender_health_after=health, applied_at=timezone.now())
    return health


def fight_winner_id(damage_taken):
    """The soldier who took less damage in this fight wins; equal damage is a draw (None)."""
    (first, first_damage), (second, second_damage) = damage_taken.items()
    if first_damage == second_damage:
        return None
    return first if first_damage < second_damage else second


def update_fight(fight_id, **fields):
    Fight.objects.filter(external_id=fight_id).update(updated_at=timezone.now(), **fields)


def health_mismatches():
    """Soldiers whose health is not drafted health minus all applied damage (floored at 0).

    Health only goes down, so the expected value does not depend on the order fights ran in.
    Any mismatch means concurrent updates were lost.
    """
    received = dict(
        FightEvent.objects.exclude(applied_at=None)
        .values("defender")
        .annotate(total=Sum("damage"))
        .values_list("defender", "total")
    )
    mismatches = []
    for soldier in Person.objects.exclude(draft_raw=None):
        expected = max(0, soldier.drafted_health - received.get(soldier.pk, 0))
        if soldier.health != expected:
            mismatches.append((soldier, expected))
    return mismatches
