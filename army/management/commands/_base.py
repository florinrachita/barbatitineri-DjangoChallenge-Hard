"""Shared helpers for commands that start background tasks and wait for them."""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.tasks import TaskResultStatus

from army.batches import KINDS, CannotStart
from army.models import TaskStatus
from army.tasks import start_download

POLL_SECONDS = 1


class TaskCommand(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=int, default=1800, help="Give up waiting after N seconds.")

    def start(self, start_function, *args):
        try:
            result = start_function(*args)
        except CannotStart as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"Enqueued task {result.id} (TASKS_MODE={settings.TASKS_MODE})")
        if settings.TASKS_MODE == "database":
            self.stdout.write("Tasks run in worker processes: start them with `manage.py run_workers`.")
        return result

    def wait_for_task(self, result, deadline):
        while not result.is_finished:
            self.check_deadline(deadline)
            time.sleep(POLL_SECONDS)
            result.refresh()
        if result.status != TaskResultStatus.SUCCESSFUL:
            errors = "\n".join(error.traceback for error in result.errors)
            raise CommandError(f"Task {result.task.name} failed:\n{errors}")
        return result.return_value

    def wait_until_finished(self, queryset, deadline, describe=None):
        """Wait until no row in queryset is pending/running, printing rows as they finish."""
        reported = set()
        while True:
            finished = queryset.exclude(pk__in=reported).exclude(status__in=[TaskStatus.PENDING, TaskStatus.RUNNING])
            for row in finished:
                reported.add(row.pk)
                if describe:
                    self.stdout.write(f"  {describe(row)}")
            if not queryset.filter(status__in=[TaskStatus.PENDING, TaskStatus.RUNNING]).exists():
                return
            self.check_deadline(deadline)
            time.sleep(POLL_SECONDS)

    def check_deadline(self, deadline):
        if time.monotonic() > deadline:
            raise CommandError("Timed out waiting for tasks (they may still be running).")

    def styled(self, ok, text):
        return (self.style.SUCCESS if ok else self.style.ERROR)(text)


class BatchDownloadCommand(TaskCommand):
    """Download every <kind> batch as a separate background task, in parallel."""

    kind_name = None  # set by subclasses: "draft" or "fight"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--batch", type=int, action="append", dest="batches",
            help="Only (re)download this batch number. Repeat for several batches.",
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        deadline = started + options["timeout"]
        kind = KINDS[self.kind_name]

        result = self.start(start_download, self.kind_name, options["batches"])
        numbers = self.wait_for_task(result, deadline)
        self.stdout.write(f"Enqueued {self.kind_name} batches: {numbers}")
        # A full run starts from a clean table and may discover extra batches via next_batch.
        batches = kind.model.objects.all()
        if options["batches"] is not None:
            batches = batches.filter(number__in=numbers)
        self.wait_until_finished(batches, deadline, self.describe)

        self.stdout.write(f"Finished in {time.monotonic() - started:.1f}s.")
        self.print_report()
        failed = list(batches.filter(status=TaskStatus.FAILED).values_list("number", flat=True))
        if failed:
            retry = " ".join(f"--batch {number}" for number in failed)
            raise CommandError(f"Batches {failed} failed. Retry with: manage.py {self.command_name()} {retry}")

    def command_name(self):
        return self.__module__.rsplit(".", 1)[-1]

    def describe(self, batch):
        text = (
            f"batch {batch.number}: {batch.status}, {batch.item_count} items, "
            f"{batch.attempts} attempt(s), {batch.duration_seconds}s"
        )
        if batch.error:
            text += f" - {batch.error}"
        return self.styled(batch.status == TaskStatus.DONE, text)

    def print_report(self):
        pass
