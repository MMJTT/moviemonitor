from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("smtp/", views.smtp_edit, name="smtp-edit"),
    path("smtp/test/", views.smtp_test, name="smtp-test"),
]
