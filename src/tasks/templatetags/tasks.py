from django import template

from ..core import get_tile

register = template.Library()


@register.inclusion_tag("tasks/tile.html")
def tasks_tile():
    """Die Kachel mit den offenen Taskwarrior-Tasks."""
    return {"tile": get_tile()}
