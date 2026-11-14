from collections import Counter

from army.models import Person

from ._base import BatchDownloadCommand


class Command(BatchDownloadCommand):
    help = "Draft registered people: one background task per /draft/<n> batch, run in parallel."
    kind_name = "draft"

    def print_report(self):
        total = Person.objects.count()
        drafted = Person.objects.exclude(rank="")
        self.stdout.write(f"Drafted {drafted.count()}/{total} people.")
        for rank, count in Counter(drafted.values_list("rank", flat=True)).most_common():
            self.stdout.write(f"  {rank}: {count}")
