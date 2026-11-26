from django.urls import path

from . import views

app_name = "tasks"
urlpatterns = [
    path("", views.tile, name="tile"),
    path("<uuid:task_uuid>/snooze/", views.snooze, name="snooze"),
    path("<uuid:task_uuid>/done/", views.done, name="done"),
]
