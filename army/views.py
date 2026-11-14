from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render

from .forms import DraftForm
from .models import DraftBatch, Person
from .tasks import draft_all


def index(request):
    """Army overview: draft batches, rank totals, soldiers, and a form to start drafting."""
    if request.method == "POST":
        form = DraftForm(request.POST)
        if form.is_valid():
            batches = form.cleaned_data["batches"]
            result = draft_all.enqueue(batch_numbers=batches)
            which = "all batches" if batches is None else f"batches {batches}"
            messages.success(request, f"Drafting started for {which} (task {result.id}).")
            return redirect("army:index")
    else:
        form = DraftForm()

    context = {
        "form": form,
        "batches": DraftBatch.objects.all(),
        "drafting": DraftBatch.objects.active().exists(),
        "rank_counts": Person.objects.exclude(rank="").values("rank").annotate(total=Count("id")).order_by("rank"),
        "people": Person.objects.all(),
        "drafted_count": Person.objects.exclude(rank="").count(),
    }
    return render(request, "army/index.html", context)
