import signal
import subprocess
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Start N `db_worker` processes for TASKS_MODE=database and wait for them. "
        "Each worker runs one task at a time, so N workers download N batches in parallel."
    )

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=10, help="Number of worker processes (default: 10).")

    def handle(self, *args, count, **options):
        if settings.TASKS_MODE != "database":
            raise CommandError("Workers are only needed with TASKS_MODE=database.")
        if count < 1:
            raise CommandError("--count must be at least 1.")

        manage_py = str(settings.BASE_DIR / "manage.py")
        workers = [
            subprocess.Popen(
                [sys.executable, manage_py, "db_worker", "--no-reload", "--worker-id", f"worker-{i}"]
            )
            for i in range(count)
        ]
        self.stdout.write(f"Started {count} workers (pids {[w.pid for w in workers]}). Ctrl+C to stop.")
        signal.signal(signal.SIGTERM, raise_keyboard_interrupt)  # `kill <pid>` stops workers too
        try:
            for worker in workers:
                worker.wait()
        except KeyboardInterrupt:
            self.stdout.write("Stopping workers...")
            for worker in workers:
                worker.send_signal(signal.SIGINT)  # db_worker finishes its current task, then exits
            for worker in workers:
                worker.wait()


def raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt
