from army.models import Fight, FightEvent

from ._base import BatchDownloadCommand


class Command(BatchDownloadCommand):
    help = "Download all fights and their events: one background task per /fight/<n> batch, run in parallel."
    kind_name = "fight"

    def print_report(self):
        self.stdout.write(f"Stored {Fight.objects.count()} fights with {FightEvent.objects.count()} events.")
