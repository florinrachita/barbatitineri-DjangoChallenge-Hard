from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from army.api import ChallengeAPIError, build_url, fetch_people
from army.models import Person

KEY_HOLDER_NAME = "Hellstorm Fireburn"


class Command(BaseCommand):
    help = "Download all people from <CHALLENGE_HOST>/people and register them."

    def handle(self, *args, **options):
        try:
            people = fetch_people()
        except ChallengeAPIError as exc:
            raise CommandError(str(exc)) from exc

        created, updated = self.save_people(people)
        self.print_summary(len(people), created, updated)
        self.print_next_step()

    @transaction.atomic
    def save_people(self, people):
        created = updated = 0
        for item in people:
            _, was_created = Person.objects.update_or_create(
                external_id=item["id"],
                defaults={
                    "name": item["name"],
                    "age": item["age"],
                    "key": item.get("key", ""),
                    "raw": item,
                },
            )
            created += was_created
            updated += not was_created
        return created, updated

    def print_summary(self, fetched, created, updated):
        self.stdout.write(f"Fetched {fetched} people: {created} created, {updated} updated.")
        stored = Person.objects.count()
        if stored == fetched:
            self.stdout.write(self.style.SUCCESS(f"OK: {stored} people in the database match the JSON."))
        else:
            self.stdout.write(self.style.WARNING(f"Mismatch: {stored} in database vs {fetched} in JSON."))

    def print_next_step(self):
        holder = Person.objects.filter(name=KEY_HOLDER_NAME).exclude(key="").first()
        if holder is None:
            self.stdout.write(self.style.WARNING(f"No '{KEY_HOLDER_NAME}' with a key was found."))
            return
        self.stdout.write(f"{KEY_HOLDER_NAME} key: {holder.key}")
        self.stdout.write(f"Next instructions: {build_url(holder.key)}")
