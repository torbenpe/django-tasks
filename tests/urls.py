from django.urls import include, path

urlpatterns = [path("tasks/", include("tasks.urls"))]
