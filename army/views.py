from django.contrib import messages
from django.db.models import Count, Prefetch
from django.shortcuts import redirect, render

from .batches import CannotStart, mark_interrupted
from .fights import health_mismatches
from .forms import BatchForm, SimulationForm
from .models import DraftBatch, Fight, FightBatch, FightEvent, Person
from .tasks import start_download, start_simulation


def index(request):
    """Army overview: draft batches, rank totals, soldiers, and a form to start drafting."""
    mark_interrupted(DraftBatch)
    form = BatchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if started := start_or_add_error(form, start_download, "draft", form.cleaned_data["batches"]):
            messages.success(request, f"Drafting started (task {started.id}).")
            return redirect("army:index")

    context = {
        "form": form,
        "batches": DraftBatch.objects.all(),
        "running": DraftBatch.objects.active().exists(),
        "rank_counts": Person.objects.exclude(rank="").values("rank").annotate(total=Count("id")).order_by("rank"),
        "people": Person.objects.all(),
        "drafted_count": Person.objects.exclude(rank="").count(),
    }
    return render(request, "army/index.html", context)


def simulate(request):
    """Download fights, replay them in parallel, and show the results."""
    mark_interrupted(FightBatch)
    mark_interrupted(Fight)
    download_form = BatchForm(request.POST if request.POST.get("action") == "download" else None, prefix="fights")
    simulation_form = SimulationForm(request.POST if request.POST.get("action") == "simulate" else None)

    if request.method == "POST":
        started = None
        if download_form.is_bound and download_form.is_valid():
            started = start_or_add_error(download_form, start_download, "fight", download_form.cleaned_data["batches"])
        elif simulation_form.is_bound and simulation_form.is_valid():
            started = start_or_add_error(simulation_form, start_simulation)
        if started:
            messages.success(request, f"Started task {started.id}.")
            return redirect("army:simulate")

    fights = Fight.objects.select_related("blue", "red", "winner").prefetch_related(
        Prefetch("events", queryset=FightEvent.objects.select_related("attacker", "defender"))
    )
    status_counts = dict(Fight.objects.values_list("status").annotate(total=Count("id")))
    done = status_counts.get(Fight.Status.DONE, 0)
    context = {
        "download_form": download_form,
        "simulation_form": simulation_form,
        "fight_batches": FightBatch.objects.all(),
        "fights": fights,
        "status_counts": [(label, status_counts.get(value, 0)) for value, label in Fight.Status.choices],
        "event_count": FightEvent.objects.count(),
        "simulated": done > 0,
        "running": FightBatch.objects.active().exists() or Fight.objects.active().exists(),
        "soldiers": Person.objects.exclude(draft_raw=None).annotate(
            fights=Count("blue_fights", distinct=True) + Count("red_fights", distinct=True),
            wins=Count("won_fights", distinct=True),
        ).order_by("-health", "external_id"),
        "mismatches": health_mismatches() if done else [],
    }
    return render(request, "army/simulate.html", context)


def start_or_add_error(form, start_function, *args):
    """Run start_function; on CannotStart show the reason on the form and return None."""
    try:
        return start_function(*args)
    except CannotStart as exc:
        form.add_error(None, str(exc))
        return None
