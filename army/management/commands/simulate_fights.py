import time

from django.core.management.base import CommandError

from army.fights import health_mismatches
from army.models import Fight, Person
from army.tasks import start_simulation

from ._base import TaskCommand


class Command(TaskCommand):
    help = "Replay every fight as its own background task (in parallel) and lower defenders' health."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--verbose-fights", action="store_true", help="Print every fight as it finishes.")

    def handle(self, *args, **options):
        started = time.monotonic()
        deadline = started + options["timeout"]

        result = self.start(start_simulation)
        fight_ids = self.wait_for_task(result, deadline)
        self.stdout.write(f"Enqueued {len(fight_ids)} fight simulations.")
        describe = self.describe if options["verbose_fights"] else None
        self.wait_until_finished(Fight.objects.all(), deadline, describe)

        self.stdout.write(f"Finished in {time.monotonic() - started:.1f}s.")
        self.print_report()

    def describe(self, fight):
        winner = fight.winner.name if fight.winner else "draw"
        text = (
            f"fight {fight.external_id}: {fight.status}, blue took {fight.blue_damage_taken}, "
            f"red took {fight.red_damage_taken}, winner: {winner}"
        )
        return self.styled(fight.status == Fight.Status.DONE, text + (f" - {fight.error}" if fight.error else ""))

    def print_report(self):
        fights = Fight.objects.all()
        done = fights.filter(status=Fight.Status.DONE)
        self.stdout.write(
            f"Fights: {done.count()} done ({done.filter(winner=None).count()} draws), "
            f"{fights.filter(status=Fight.Status.FAILED).count()} failed."
        )
        soldiers = Person.objects.exclude(draft_raw=None)
        self.stdout.write(
            f"Soldiers: {soldiers.filter(health__gt=0).count()} still standing, "
            f"{soldiers.filter(health=0).count()} down."
        )
        mismatches = health_mismatches()
        if mismatches:
            for soldier, expected in mismatches:
                self.stderr.write(f"  {soldier}: health {soldier.health}, expected {expected}")
            raise CommandError(f"Health check failed for {len(mismatches)} soldiers (lost updates).")
        self.stdout.write(self.styled(True, "Health check OK: every soldier's health = drafted health - damage taken."))
        failed = list(fights.filter(status=Fight.Status.FAILED).values_list("external_id", flat=True))
        if failed:
            raise CommandError(f"Fights {failed} failed.")
