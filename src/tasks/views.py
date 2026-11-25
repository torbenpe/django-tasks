from django.shortcuts import render
from django.views.decorators.http import require_safe

from . import core


@require_safe
def tile(request):
    context = core.get_tile(show_all=request.GET.get("all") == "1")
    return render(request, "tasks/tile.html", {"tile": context})
