from django.contrib.auth import views as auth_views
from django.urls import path

from core import views
from core.forms import EmailAuthenticationForm

app_name = "core"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            authentication_form=EmailAuthenticationForm,
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("invite/<str:token>/", views.invite_register, name="invite-register"),
    path("", views.dashboard, name="dashboard"),
    path("invites/", views.invitations, name="invitations"),
    path(
        "invites/<int:invitation_id>/revoke/",
        views.invitation_revoke,
        name="invitation-revoke",
    ),
    path("users/", views.users, name="users"),
    path("users/<int:user_id>/toggle/", views.user_toggle, name="user-toggle"),
    path("settings/", views.settings_edit, name="settings"),
    path("mail/", views.mail_edit, name="mail-edit"),
    path("mail/test/", views.mail_test, name="mail-test"),
    path("smtp/", views.legacy_smtp_redirect, name="smtp-edit"),
    path("smtp/test/", views.legacy_smtp_redirect, name="smtp-test"),
    path("task/new/", views.task_preview, name="task-preview"),
    path("task/confirm/", views.task_confirm, name="task-confirm"),
    path("task/<uuid:task_id>/", views.task_detail, name="task-detail"),
    path("task/<uuid:task_id>/pause/", views.task_pause, name="task-pause"),
    path("task/<uuid:task_id>/resume/", views.task_resume, name="task-resume"),
    path("task/<uuid:task_id>/cancel/", views.task_cancel, name="task-cancel"),
    path("task/<uuid:task_id>/run-now/", views.task_run_now, name="task-run-now"),
]
