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
@pytest.mark.parametrize(
    ("smtp_error", "expected_summary"),
    [
        (
            smtplib.SMTPAuthenticationError(535, b"authorization-code is invalid"),
            "SMTPAuthenticationError (535)",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {"receiver@example.com": (550, b"private recipient response")}
            ),
            "SMTPRecipientsRefused",
        ),
    ],
)
def test_configuration_smtp_error_parks_notification_and_unverifies_smtp(
    opening_notification, verified_smtp, mocker, smtp_error, expected_summary
):
    """Discarding an opening after a repairable SMTP configuration error must fail this test."""
    verified_smtp.verified_at = timezone.now()
    verified_smtp.save()
    mocker.patch(
        "core.services.notifications.send_message",
        side_effect=smtp_error,
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    verified_smtp.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.retry_count == 0
    assert opening_notification.last_error == expected_summary
    assert "authorization-code" not in opening_notification.last_error
    assert verified_smtp.is_verified is False
    assert verified_smtp.verified_at is None


@pytest.mark.django_db
def test_delivery_claim_with_unverified_smtp_returns_to_pending(
    opening_notification, verified_smtp, mocker
):
    """Turning an unverified configuration into permanent notification loss must fail this test."""
    verified_smtp.is_verified = False
    verified_smtp.save()
    send = mocker.patch("core.services.notifications.send_message")

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == "smtp-not-verified"
    send.assert_not_called()


@pytest.mark.django_db
def test_dispatch_skips_pending_notifications_while_smtp_is_unverified(
    opening_notification, verified_smtp, mocker
):
    """Reclaiming the same parked notification every scheduler second must fail this test."""
    verified_smtp.is_verified = False
    verified_smtp.save()
    send = mocker.patch("core.services.notifications.send_message")

    assert dispatch_due_notifications() == 0
    assert dispatch_due_notifications() == 0

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.last_error == ""
    send.assert_not_called()


@pytest.mark.django_db
def test_malformed_local_key_parks_claimed_notification_for_credential_repair(
    opening_notification, verified_smtp, settings, tmp_path, mocker
):
    """Leaving a notification SENDING after malformed local-key decryption must fail this test."""
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"
    settings.TICKETWATCH_KEY_FILE.write_bytes(b"truncated-local-key")
    transport = mocker.patch("core.services.smtp._connect").return_value

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    verified_smtp.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.next_attempt_at is None
    assert opening_notification.last_error == "smtp-credential-unavailable"
    assert verified_smtp.is_verified is False
    transport.login.assert_not_called()


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


@pytest.mark.django_db
def test_cancelled_task_permanently_stops_pending_opening_mail(
    opening_notification, verified_smtp, mocker
):
    """Sending a pending opening notification after task cancellation must fail this test."""
    opening_notification.task.status = MonitorTask.Status.CANCELLED
    opening_notification.task.save()
    send = mocker.patch("core.services.notifications.send_message", return_value="accepted")

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.last_error == "task-no-longer-detected"
    assert opening_notification.sent_at is None
    send.assert_not_called()
