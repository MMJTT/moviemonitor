from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import AgentMailConfig, MonitorTask, Notification
from core.services.agent_mail import (
    AgentMailAuthError,
    AgentMailPermanentError,
    AgentMailTemporaryError,
)
from core.services.notifications import deliver_notification, dispatch_due_notifications

SENDER = "mijiatong@agent.qq.com"
RECIPIENT = "850634546@qq.com"


@pytest.fixture
def verified_mail(db):
    config = AgentMailConfig.get_solo()
    config.recipient_email = RECIPIENT
    config.is_verified = True
    config.verified_at = timezone.now()
    config.save()
    return config


@pytest.mark.django_db
def test_successful_agent_mail_opening_notification_completes_task(
    opening_notification, verified_mail, mocker
):
    send = mocker.patch(
        "core.services.notifications.send_agent_mail",
        return_value="queued",
        create=True,
    )
    now = timezone.now()

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.transport_response == "queued"
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED
    recipient, subject, body = send.call_args.args
    assert recipient == RECIPIENT
    assert "已在" in subject
    assert opening_notification.task.movie_name in subject
    assert opening_notification.task.cinema_name in body
    assert opening_notification.task.booking_url in body
    assert SENDER not in body


@pytest.mark.django_db
def test_temporary_agent_mail_error_schedules_retry(
    opening_notification, verified_mail, mocker
):
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailTemporaryError("agent-mail-network-error"),
        create=True,
    )
    now = timezone.now()

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.retry_count == 1
    assert opening_notification.next_attempt_at == now + timedelta(minutes=1)
    assert opening_notification.last_error == "agent-mail-network-error"


@pytest.mark.django_db
def test_agent_mail_auth_error_parks_notification_and_unverifies_config(
    opening_notification, verified_mail, mocker
):
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailAuthError("agent-mail-auth-required"),
        create=True,
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    verified_mail.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == "agent-mail-auth-required"
    assert verified_mail.is_verified is False
    assert verified_mail.verified_at is None


@pytest.mark.django_db
def test_permanent_agent_mail_rejection_is_not_retried(
    opening_notification, verified_mail, mocker
):
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailPermanentError("agent-mail-recipient-rejected"),
        create=True,
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == "agent-mail-recipient-rejected"
    assert opening_notification.task.status == MonitorTask.Status.DETECTED


@pytest.mark.django_db
def test_dispatch_requires_verified_agent_mail(opening_notification, mocker):
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued", create=True
    )

    assert dispatch_due_notifications() == 0
    send.assert_not_called()
