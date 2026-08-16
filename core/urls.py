from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("smtp/", views.smtp_edit, name="smtp-edit"),
    path("smtp/test/", views.smtp_test, name="smtp-test"),
    path("task/new/", views.task_preview, name="task-preview"),
    path("task/confirm/", views.task_confirm, name="task-confirm"),
]
