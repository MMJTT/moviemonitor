from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import MonitorTask, Notification
from core.services.agent_mail import AgentMailTemporaryError
from core.services.notifications import deliver_notification, dispatch_due_notifications


@pytest.mark.django_db
def test_five_scheduled_retries_end_in_failed(opening_notification, verified_smtp, mocker):
    opening_notification.retry_count = 5
    opening_notification.save()
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailTemporaryError("agent-mail-network-error"),
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.FAILED
    assert opening_notification.retry_count == 5
    assert opening_notification.next_attempt_at is None


@pytest.mark.django_db
def test_delivery_claim_with_unverified_mail_returns_to_pending(
    opening_notification, verified_smtp, mocker
):
    verified_smtp.is_verified = False
    verified_smtp.save()
    send = mocker.patch("core.services.notifications.send_agent_mail")

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == "agent-mail-not-verified"
    send.assert_not_called()


@pytest.mark.django_db
def test_dispatch_skips_pending_notifications_while_mail_is_unverified(
    opening_notification, verified_smtp, mocker
):
    verified_smtp.is_verified = False
    verified_smtp.save()
    send = mocker.patch("core.services.notifications.send_agent_mail")

    assert dispatch_due_notifications() == 0
    assert dispatch_due_notifications() == 0

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.last_error == ""
    send.assert_not_called()


@pytest.mark.django_db
def test_dispatch_sends_only_due_pending_notifications(task_factory, verified_smtp, mocker):
    now = timezone.now()
    due_task = task_factory(status=MonitorTask.Status.CANCELLED)
    future_task = task_factory(status=MonitorTask.Status.CANCELLED)
    sending_task = task_factory(status=MonitorTask.Status.CANCELLED)
    due = Notification.objects.create(
        task=due_task,
        notification_type=Notification.Type.EXPIRY,
        next_attempt_at=now,
    )
    Notification.objects.create(
        task=future_task,
        notification_type=Notification.Type.EXPIRY,
        next_attempt_at=now + timedelta(minutes=1),
    )
    sending = Notification.objects.create(
        task=sending_task,
        notification_type=Notification.Type.EXPIRY,
        status=Notification.Status.SENDING,
    )
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )

    assert dispatch_due_notifications(now=now) == 1

    due.refresh_from_db()
    sending.refresh_from_db()
    assert due.status == Notification.Status.SENT
    assert sending.status == Notification.Status.SENDING
    assert send.call_count == 1


@pytest.mark.django_db
def test_preexisting_sending_notification_is_never_reclaimed(
    opening_notification, verified_smtp, mocker
):
    opening_notification.status = Notification.Status.SENDING
    opening_notification.save()
    send = mocker.patch("core.services.notifications.send_agent_mail")

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENDING
    send.assert_not_called()


@pytest.mark.django_db
def test_cancelled_task_permanently_stops_pending_opening_mail(
    opening_notification, verified_smtp, mocker
):
    opening_notification.task.status = MonitorTask.Status.CANCELLED
    opening_notification.task.save()
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.last_error == "task-no-longer-detected"
    assert opening_notification.sent_at is None
    send.assert_not_called()
