from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from . import core

TEMPLATE = "tasks/tile.html"


@require_safe
def tile(request):
    context = core.get_tile(show_all=request.GET.get("all") == "1")
    return render(request, TEMPLATE, {"tile": context})


def _after_post(request, flash):
    # Ohne HTMX (kein JavaScript) zurück zur Seite mit der Kachel statt eines nackten Partials
    if request.headers.get("HX-Request") != "true":
        return redirect(getattr(settings, "TASKS_HOME_URL", "/"))
    context = core.get_tile(show_all=request.POST.get("all") == "1", flash=flash)
    return render(request, TEMPLATE, {"tile": context})


@require_POST
def snooze(request, task_uuid):
    try:
        due = core.snooze_task(str(task_uuid))
    except core.ReadOnly:
        flash = "Verschieben geht hier nicht: weder task noch Tasks-Brücke erreichbar."
    except core.SourceError as exc:
        flash = f"Nicht verschoben – {exc}"
    else:
        flash = f"Verschoben auf {core.date_label(core.timezone.localtime(due).date())}." if due else ""
    return _after_post(request, flash)


@require_POST
def done(request, task_uuid):
    try:
        description = core.complete_task(str(task_uuid))
    except core.ReadOnly:
        flash = "Abhaken geht hier nicht: weder task noch Tasks-Brücke erreichbar."
    except core.SourceError as exc:
        flash = f"Nicht abgehakt – {exc}"
    else:
        flash = f"Erledigt: {description}" if description else "Erledigt."
    return _after_post(request, flash)
