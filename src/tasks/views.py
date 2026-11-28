from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from . import core

TEMPLATE = "tasks/tile.html"


@require_safe
def tile(request):
    context = core.get_tile(show_all=request.GET.get("all") == "1", edit=request.GET.get("edit") == "neu")
    return render(request, TEMPLATE, {"tile": context})


def _after_post(request, flash="", **kwargs):
    # Ohne HTMX (kein JavaScript) zurück zur Seite mit der Kachel statt eines nackten Partials
    if request.headers.get("HX-Request") != "true":
        return redirect(getattr(settings, "TASKS_HOME_URL", "/"))
    context = core.get_tile(show_all=request.POST.get("all") == "1", flash=flash, **kwargs)
    return render(request, TEMPLATE, {"tile": context})


@require_POST
def add(request):
    fields, errors = core.parse_new(request.POST)
    form = {key: request.POST.get(key, "") for key in core.NEW_FIELDS}
    if errors:
        return _after_post(request, edit=True, form=form, errors=errors)
    try:
        task_id, description = core.add_task(fields)
    except core.ReadOnly:
        flash = "Anlegen geht hier nicht: weder task noch Tasks-Brücke erreichbar."
    except core.SourceError as exc:
        # Formular offen lassen, damit die Eingabe nicht verloren geht
        return _after_post(request, edit=True, form=form, message=f"Nicht angelegt – {exc}")
    else:
        flash = f"Angelegt: {f'{task_id} ' if task_id else ''}{description}"
    return _after_post(request, flash)


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
