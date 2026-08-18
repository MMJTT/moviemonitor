from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from core.models import MonitorTask, Notification
from core.services.agent_mail import AgentMailTemporaryError
from core.services.notifications import deliver_notification, expire_due_task
from core.worker import run_worker_due_work

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.django_db
@freeze_time("2026-08-21 00:00:00", tz_offset=8)
def test_expiry_is_timezone_aware_and_idempotent(active_task):
    """Using UTC date or creating duplicate expiry facts must fail this test."""
    active_task.show_date = date(2026, 8, 20)
    active_task.save()

    assert expire_due_task() is True
    assert expire_due_task() is False

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.EXPIRED
    assert active_task.expired_at is not None
    assert active_task.next_check_at is None
    assert active_task.notifications.filter(
        notification_type=Notification.Type.EXPIRY
    ).count() == 1


@pytest.mark.django_db
def test_show_date_does_not_expire_until_next_local_day(active_task):
    """Expiring during the target show date must fail this test."""
    now = datetime(2026, 8, 20, 23, 59, tzinfo=SHANGHAI)
    active_task.show_date = date(2026, 8, 20)
    active_task.save()

    assert expire_due_task(now=now) is False

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert not active_task.notifications.exists()


@pytest.mark.django_db
def test_all_past_date_tasks_expire_before_live_polling(task_factory):
    """Expiring only one task would leave another past-date task eligible for polling."""
    now = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    first = task_factory(show_date=date(2026, 8, 20))
    second = task_factory(
        show_date=date(2026, 8, 19),
        movie_id="2222222",
        movie_name="第二部电影",
        query_key="maoyan:10:expired-second",
        cinema_name="另一家影院",
        normalized_cinema_name="另一家影院",
    )

    assert expire_due_task(now=now) is True

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == MonitorTask.Status.EXPIRED
    assert second.status == MonitorTask.Status.EXPIRED
    assert Notification.objects.filter(
        task__in=[first, second], notification_type=Notification.Type.EXPIRY
    ).count() == 2


@pytest.mark.django_db
def test_expiry_skips_detected_task_with_pending_opening(opening_notification, mocker):
    """Expiring a known opening would replace the true result with a false expiry."""
    now = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    opening_notification.task.show_date = date(2026, 8, 20)
    opening_notification.task.save()
    send = mocker.patch("core.services.notifications.send_agent_mail")

    assert expire_due_task(now=now) is False
    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.PENDING
    assert opening_notification.task.status == MonitorTask.Status.DETECTED
    assert opening_notification.task.notifications.filter(
        notification_type=Notification.Type.EXPIRY
    ).count() == 0
    send.assert_not_called()


@pytest.mark.django_db
def test_direct_opening_delivery_after_boundary_sends_known_opening(
    opening_notification, verified_smtp, mocker
):
    """The show-date boundary must not discard an opening detected before midnight."""
    now = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    opening_notification.task.show_date = date(2026, 8, 20)
    opening_notification.task.save()
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED
    assert opening_notification.task.notifications.filter(
        notification_type=Notification.Type.EXPIRY
    ).count() == 0
    send.assert_called_once()


@pytest.mark.django_db
def test_successful_expiry_mail_keeps_task_expired(active_task, verified_smtp, mocker):
    """Completing an expired task after expiry mail acceptance must fail this test."""
    active_task.status = MonitorTask.Status.EXPIRED
    active_task.expired_at = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    active_task.save()
    notification = Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.EXPIRY,
    )
    mocker.patch("core.services.notifications.send_agent_mail", return_value="queued")

    deliver_notification(notification.pk)

    active_task.refresh_from_db()
    notification.refresh_from_db()
    assert notification.status == Notification.Status.SENT
    assert active_task.status == MonitorTask.Status.EXPIRED


@pytest.mark.django_db
def test_detected_before_midnight_survives_retry_and_sends_one_opening(
    opening_notification, verified_smtp, mocker
):
    """Expiring DETECTED would replace a known opening with a false expiry alert."""
    detected_at = datetime(2026, 8, 20, 23, 59, tzinfo=SHANGHAI)
    retry_at = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    task = opening_notification.task
    task.show_date = date(2026, 8, 20)
    task.detected_at = detected_at
    task.save(update_fields=["show_date", "detected_at"])
    send = mocker.patch(
        "core.services.notifications.send_agent_mail",
        side_effect=[AgentMailTemporaryError("private endpoint"), "queued"],
    )
    mocker.patch(
        "core.services.mail_verification.verify_agent_mail",
        return_value="mijiatong@agent.qq.com",
    )

    deliver_notification(opening_notification.pk, now=detected_at)
    result = run_worker_due_work(now=retry_at)

    opening_notification.refresh_from_db()
    task.refresh_from_db()
    assert result["expired"] is False
    assert result["notifications"] == 1
    assert task.status == MonitorTask.Status.COMPLETED
    assert task.expired_at is None
    assert opening_notification.status == Notification.Status.SENT
    assert send.call_count == 2
    assert task.notifications.filter(
        notification_type=Notification.Type.OPENING
    ).count() == 1
    assert task.notifications.filter(
        notification_type=Notification.Type.EXPIRY
    ).count() == 0
