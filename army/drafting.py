"""Drafting: storing /draft/<n> soldiers (rank and health) on registered people."""

import math

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Person


def estimated_batch_count():
    return max(1, math.ceil(Person.objects.count() / settings.DRAFT_BATCH_SIZE))


@transaction.atomic
def save_soldiers(batch_number, items):
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
