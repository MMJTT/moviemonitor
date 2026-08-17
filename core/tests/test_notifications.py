from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import MonitorTask, Notification
from core.services.agent_mail import AgentMailTemporaryError, AgentMailUncertainError
from core.services.notifications import (
    deliver_notification,
    dispatch_due_notifications,
    mark_uncertain_sending_notifications,
)


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


@pytest.mark.django_db
def test_worker_startup_parks_preexisting_sending_notification(opening_notification):
    """Leaving crash-interrupted sends claimable could duplicate accepted email."""
    opening_notification.status = Notification.Status.SENDING
    opening_notification.next_attempt_at = timezone.now()
    opening_notification.save(update_fields=["status", "next_attempt_at"])

    assert mark_uncertain_sending_notifications() == 1
    assert mark_uncertain_sending_notifications() == 0

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.NEEDS_REVIEW
    assert opening_notification.next_attempt_at is None
    assert (
        opening_notification.last_error
        == "agent-mail-result-unknown-after-restart"
    )


@pytest.mark.django_db
def test_uncertain_delivery_is_parked_without_automatic_retry(
    opening_notification, verified_smtp, mocker
):
    """Returning an ambiguous live send to PENDING could send it twice."""
    mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=AgentMailUncertainError("agent-mail-result-unknown"),
    )

    deliver_notification(opening_notification.pk)

    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.NEEDS_REVIEW
    assert opening_notification.next_attempt_at is None
    assert opening_notification.retry_count == 0
    assert opening_notification.last_error == "agent-mail-result-unknown"


@pytest.mark.django_db
def test_system_alert_message_contains_only_sanitized_task_context(
    task_factory, verified_smtp, mocker
):
    """Including stored source/provider detail could leak remote content by email."""
    task = task_factory(
        status=MonitorTask.Status.ERROR,
        source_url="https://private.example.test/secret-response",
        last_error="猫眼页面结构无法验证",
        next_check_at=None,
    )
    alert = Notification.objects.create(
        task=task,
        notification_type=Notification.Type.SYSTEM_ALERT,
    )
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )

    deliver_notification(alert.pk)

    assert send.call_args.args[1] == "[TicketWatch] 监控任务需要处理"
    assert send.call_args.args[2].splitlines() == [
        f"电影：{task.movie_name}",
        f"日期：{task.show_date.isoformat()}",
        f"城市：{task.city_name}",
        f"影院：{task.cinema_name}",
        "错误类别：猫眼页面结构无法验证",
        f"任务详情：{reverse('core:task-detail', args=[task.pk])}",
    ]
    assert "private.example.test" not in send.call_args.args[2]
