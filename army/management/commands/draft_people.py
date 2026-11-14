import time
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.tasks import TaskResultStatus

from army.models import DraftBatch, Person
from army.tasks import DraftingInProgress, start_drafting

POLL_SECONDS = 1


class Command(BaseCommand):
    help = "Draft registered people: one background task per /draft/<n> batch, run in parallel."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch", type=int, action="append", dest="batches",
            help="Only (re)draft this batch number. Repeat for several batches.",
        )
        parser.add_argument("--timeout", type=int, default=1800, help="Give up waiting after N seconds.")

    def handle(self, *args, **options):
        started = time.monotonic()
        deadline = started + options["timeout"]

        try:
            result = start_drafting(options["batches"])
        except DraftingInProgress as exc:
            raise CommandError(f"{exc} Wait for them to finish, or for them to be marked interrupted.")
        self.stdout.write(f"Enqueued draft_all task {result.id} (TASKS_MODE={settings.TASKS_MODE})")
        if settings.TASKS_MODE == "database":
            self.stdout.write("Tasks run in worker processes: start them with `manage.py run_workers`.")

        self.wait_for_task(result, deadline)
        numbers = result.return_value
        self.stdout.write(f"Enqueued batches: {numbers}")
        # A full run starts from a clean table and may discover extra batches via next_batch.
        batches = DraftBatch.objects.all()
        if options["batches"] is not None:
            batches = batches.filter(number__in=numbers)
        self.wait_for_batches(batches, deadline)

        self.print_report(time.monotonic() - started)
        failed = list(batches.filter(status=DraftBatch.Status.FAILED).values_list("number", flat=True))
        if failed:
            retry = " ".join(f"--batch {number}" for number in failed)
            raise CommandError(f"Batches {failed} failed. Retry with: manage.py draft_people {retry}")

    def wait_for_task(self, result, deadline):
        while not result.is_finished:
            self.check_deadline(deadline)
            time.sleep(POLL_SECONDS)
            result.refresh()
        if result.status != TaskResultStatus.SUCCESSFUL:
            errors = "\n".join(error.traceback for error in result.errors)
            raise CommandError(f"draft_all failed:\n{errors}")

    def wait_for_batches(self, batches, deadline):
        reported = set()
        while True:
            for batch in batches.exclude(number__in=reported).exclude(
                status__in=[DraftBatch.Status.PENDING, DraftBatch.Status.RUNNING]
            ):
                reported.add(batch.number)
                self.stdout.write(f"  {self.describe(batch)}")
            if not batches.active().exists():
                return
            self.check_deadline(deadline)
            time.sleep(POLL_SECONDS)

    def check_deadline(self, deadline):
        if time.monotonic() > deadline:
            raise CommandError("Timed out waiting for draft tasks (they may still be running).")

    def describe(self, batch):
        text = (
            f"batch {batch.number}: {batch.status}, {batch.drafted_count} drafted, "
            f"{batch.attempts} attempt(s), {batch.duration_seconds}s"
        )
        if batch.error:
            text += f" - {batch.error}"
        style = self.style.SUCCESS if batch.status == DraftBatch.Status.DONE else self.style.ERROR
        return style(text)

    def print_report(self, elapsed):
        total = Person.objects.count()
        drafted = Person.objects.exclude(rank="").count()
        ranks = Counter(Person.objects.exclude(rank="").values_list("rank", flat=True))
        self.stdout.write(f"Finished in {elapsed:.1f}s. Drafted {drafted}/{total} people.")
        for rank, count in ranks.most_common():
            self.stdout.write(f"  {rank}: {count}")
