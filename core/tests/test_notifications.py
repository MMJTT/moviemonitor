import smtplib
from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import MonitorTask, Notification
from core.services.notifications import deliver_notification, dispatch_due_notifications


@pytest.mark.django_db
def test_successful_opening_mail_completes_task(opening_notification, verified_smtp, mocker):
    """Failing to persist SMTP acceptance and task completion must fail this test."""
    captured = {}

    def accept_message(config, message):
        captured["config"] = config
        captured["message"] = message
        return "<accepted@ticketwatch.local>"

    mocker.patch("core.services.notifications.send_message", side_effect=accept_message)
    now = timezone.now()

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    message = captured["message"]
    body = message.get_content()
    assert captured["config"].pk == verified_smtp.pk
    assert message["Message-ID"] == f"<opening-{opening_notification.pk}@ticketwatch.local>"
    assert message["From"] == verified_smtp.from_email
    assert message["To"] == verified_smtp.recipient_email
    assert opening_notification.task.movie_name in body
    assert opening_notification.task.city_name in body
    assert opening_notification.task.cinema_name in body
    assert opening_notification.task.show_date.isoformat() in body
    assert opening_notification.task.source_url in body
    assert opening_notification.task.booking_url in body
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.smtp_response == "accepted"
    assert opening_notification.sent_at == now
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED
    assert opening_notification.task.notified_at == now
    assert opening_notification.task.completed_at == now


@pytest.mark.django_db
def test_temporary_smtp_error_is_sanitized_and_schedules_durable_retry(
    opening_notification, verified_smtp, mocker
):
    """Dropping retry state or persisting an SMTP response body must fail this test."""
    now = timezone.now()
    mocker.patch(
        "core.services.notifications.send_message",
        side_effect=smtplib.SMTPServerDisconnected("authorization-code full body"),
    )

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.retry_count == 1
    assert opening_notification.next_attempt_at == now + timedelta(minutes=1)
    assert opening_notification.last_error == "SMTPServerDisconnected"
    assert "authorization-code" not in opening_notification.last_error
    assert opening_notification.task.status == MonitorTask.Status.DETECTED


@pytest.mark.django_db
def test_five_scheduled_retries_end_in_failed(opening_notification, verified_smtp, mocker):
    """Scheduling a sixth retry after five durable retries must fail this test."""
    opening_notification.retry_count = 5
    opening_notification.save()
    mocker.patch(
        "core.services.notifications.send_message",
        side_effect=smtplib.SMTPServerDisconnected("private response"),
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.FAILED
    assert opening_notification.retry_count == 5
    assert opening_notification.next_attempt_at is None


@pytest.mark.django_db
def test_authentication_error_permanently_fails_and_unverifies_smtp(
    opening_notification, verified_smtp, mocker
):
    """Retrying invalid credentials or exposing their SMTP response must fail this test."""
    mocker.patch(
        "core.services.notifications.send_message",
        side_effect=smtplib.SMTPAuthenticationError(535, b"authorization-code is invalid"),
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    verified_smtp.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.last_error == "SMTPAuthenticationError (535)"
    assert "authorization-code" not in opening_notification.last_error
    assert verified_smtp.is_verified is False


@pytest.mark.django_db
def test_dispatch_sends_only_due_pending_notifications(task_factory, verified_smtp, mocker):
    """Sending future, failed, or already-claimed rows must fail this test."""
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
    send = mocker.patch("core.services.notifications.send_message", return_value="accepted")

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
    """Reclaiming an uncertain SMTP send and delivering it twice must fail this test."""
    opening_notification.status = Notification.Status.SENDING
    opening_notification.save()
    send = mocker.patch("core.services.notifications.send_message")

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENDING
    send.assert_not_called()
