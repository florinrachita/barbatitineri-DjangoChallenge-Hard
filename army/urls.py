from django.urls import path

from . import views

app_name = "army"

urlpatterns = [
    path("", views.index, name="index"),
    path("simulate", views.simulate, name="simulate"),
]
