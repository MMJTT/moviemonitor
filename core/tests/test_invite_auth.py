import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import AgentMailConfig, Invitation, MonitorTask, Notification
from core.services.invitations import issue_invitation, token_digest
from core.services.notifications import deliver_notification, dispatch_due_notifications
from core.services.tasks import TaskCreationError, create_task

pytestmark = pytest.mark.django_db


@pytest.fixture
def member(django_user_model):
    return django_user_model.objects.create_user(
        username="member@example.com",
        email="member@example.com",
        password="correct-horse-battery-staple",
    )


def test_anonymous_dashboard_redirects_to_login(anonymous_client):
    response = anonymous_client.get(reverse("core:dashboard"))

    assert response.status_code == 302
    assert response.url.startswith(f"{reverse('core:login')}?next=")


def test_staff_can_issue_hashed_one_time_invitation(client, owner_user):
    response = client.post(
        reverse("core:invitations"),
        {"email": "New.Member@Example.com"},
    )

    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    invitation = Invitation.objects.get()
    assert invitation.email == "new.member@example.com"
    body = response.content.decode()
    assert invitation.token_digest not in body
    token = re.search(r"/invite/([^/]+)/", body).group(1)
    assert token_digest(token) == invitation.token_digest
    assert Invitation.objects.filter(token_digest=token).exists() is False


def test_reissuing_invitation_revokes_the_previous_token(owner_user):
    first = issue_invitation("member@example.com", owner_user)
    second = issue_invitation("member@example.com", owner_user)

    first.invitation.refresh_from_db()
    assert first.invitation.revoked_at is not None
    assert second.invitation.revoked_at is None
    assert Client().get(reverse("core:invite-register", args=[first.token])).status_code == 410


def test_staff_can_revoke_unused_invitation(client, owner_user):
    issued = issue_invitation("member@example.com", owner_user)

    response = client.post(
        reverse("core:invitation-revoke", args=[issued.invitation.pk])
    )

    assert response.status_code == 302
    issued.invitation.refresh_from_db()
    assert issued.invitation.revoked_at is not None
    assert Client().get(
        reverse("core:invite-register", args=[issued.token])
    ).status_code == 410


def test_invited_user_registers_once_and_is_logged_in(owner_user):
    issued = issue_invitation("member@example.com", owner_user)
    anonymous = Client()
    path = reverse("core:invite-register", args=[issued.token])

    response = anonymous.post(
        path,
        {
            "email": "ignored@example.com",
            "password1": "correct-horse-battery-staple",
            "password2": "correct-horse-battery-staple",
        },
    )

    assert response.status_code == 302
    assert response.url == reverse("core:dashboard")
    user = get_user_model().objects.get(email="member@example.com")
    assert int(anonymous.session["_auth_user_id"]) == user.pk
    issued.invitation.refresh_from_db()
    assert issued.invitation.accepted_by == user
    assert issued.invitation.accepted_at is not None
    anonymous.post(reverse("core:logout"))
    assert anonymous.get(path).status_code == 410


@pytest.mark.parametrize(
    "url_name",
    ["core:invitations", "core:users", "core:mail-edit", "core:settings"],
)
def test_regular_user_cannot_open_admin_pages(member, url_name):
    regular = Client()
    regular.force_login(member)

    assert regular.get(reverse(url_name)).status_code == 403


def test_users_only_see_and_control_their_own_tasks(
    client, member, task_factory
):
    owner_task = task_factory(movie_name="管理员电影")
    member_task = task_factory(
        owner=member,
        movie_id="222",
        movie_name="成员电影",
        query_key="maoyan:10:member",
        cinema_name="成员影院",
        normalized_cinema_name="成员影院",
    )
    regular = Client()
    regular.force_login(member)

    body = regular.get(reverse("core:dashboard")).content.decode()
    assert member_task.movie_name in body
    assert owner_task.movie_name not in body
    assert regular.get(reverse("core:task-detail", args=[owner_task.pk])).status_code == 404
    for url_name in (
        "core:task-pause",
        "core:task-resume",
        "core:task-cancel",
        "core:task-run-now",
    ):
        assert regular.post(reverse(url_name, args=[owner_task.pk])).status_code == 404


def test_same_target_is_allowed_for_two_different_users(
    member, owner_user, signed_preview, verified_smtp, mocker
):
    mocker.patch("core.services.tasks.enqueue_immediate_check")

    first = create_task(signed_preview, "", "同一家影院", owner=owner_user)
    second = create_task(signed_preview, "", "同一家影院", owner=member)

    assert first.owner == owner_user
    assert second.owner == member
    assert MonitorTask.objects.count() == 2


def test_disabling_user_pauses_checks_and_blocks_pending_delivery(
    client, member, task_factory, verified_smtp, mocker
):
    monitoring = task_factory(owner=member)
    task = task_factory(
        owner=member,
        movie_id="222",
        query_key="maoyan:10:detected-member",
        cinema_name="另一家影院",
        normalized_cinema_name="另一家影院",
    )
    task.status = MonitorTask.Status.DETECTED
    task.detected_at = timezone.now()
    task.save(update_fields=["status", "detected_at"])
    notification = Notification.objects.create(
        task=task,
        notification_type=Notification.Type.OPENING,
    )
    send = mocker.patch("core.services.notifications.send_agent_mail")

    response = client.post(reverse("core:user-toggle", args=[member.pk]))

    assert response.status_code == 302
    member.refresh_from_db()
    monitoring.refresh_from_db()
    assert member.is_active is False
    assert monitoring.status == MonitorTask.Status.PAUSED
    assert monitoring.next_check_at is None
    assert dispatch_due_notifications() == 0
    notification.refresh_from_db()
    assert notification.status == Notification.Status.PENDING
    send.assert_not_called()


def test_worker_sends_each_task_to_its_active_owner(
    member, task_factory, verified_smtp, mocker
):
    task = task_factory(owner=member, status=MonitorTask.Status.DETECTED)
    task.detected_at = timezone.now()
    task.booking_url = "https://www.maoyan.com/cinema/37534"
    task.save(update_fields=["detected_at", "booking_url"])
    notification = Notification.objects.create(
        task=task,
        notification_type=Notification.Type.OPENING,
    )
    send = mocker.patch(
        "core.services.notifications.send_agent_mail",
        return_value="queued",
    )

    deliver_notification(notification.pk)

    assert send.call_args.args[0] == member.email
    notification.refresh_from_db()
    assert notification.status == Notification.Status.SENT


def test_per_user_active_task_quota_is_enforced_atomically(
    settings, member, signed_preview, verified_smtp, mocker
):
    settings.MAX_ACTIVE_TASKS_PER_USER = 1
    mocker.patch("core.services.tasks.enqueue_immediate_check")
    create_task(signed_preview, "", "第一家影院", owner=member)

    with pytest.raises(TaskCreationError, match="最多同时监控 1 个目标"):
        create_task(signed_preview, "", "第二家影院", owner=member)

    assert MonitorTask.objects.filter(owner=member).count() == 1


def test_expired_invitation_cannot_register(owner_user):
    issued = issue_invitation("expired@example.com", owner_user)
    Invitation.objects.filter(pk=issued.invitation.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    response = Client().get(reverse("core:invite-register", args=[issued.token]))

    assert response.status_code == 410


def test_login_normalizes_email_case(member):
    anonymous = Client()

    response = anonymous.post(
        reverse("core:login"),
        {"username": "MEMBER@EXAMPLE.COM", "password": "correct-horse-battery-staple"},
    )

    assert response.status_code == 302
    assert int(anonymous.session["_auth_user_id"]) == member.pk


def test_nonstaff_unverified_mail_redirect_does_not_expose_admin_page(member):
    AgentMailConfig.objects.all().update(is_verified=False)
    regular = Client()
    regular.force_login(member)

    response = regular.get(reverse("core:task-preview"))

    assert response.status_code == 302
    assert response.url == reverse("core:dashboard")
