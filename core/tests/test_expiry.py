from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from core.models import MonitorTask, Notification
from core.services.notifications import deliver_notification, expire_due_task

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
def test_expiry_wins_over_pending_opening_delivery(opening_notification, mocker):
    """Allowing a pending opening email to send after the date boundary must fail this test."""
    now = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    opening_notification.task.show_date = date(2026, 8, 20)
    opening_notification.task.save()
    send = mocker.patch("core.services.notifications.send_agent_mail")

    assert expire_due_task(now=now) is True
    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.last_error == "expired-before-delivery"
    assert opening_notification.task.status == MonitorTask.Status.EXPIRED
    assert opening_notification.task.notifications.filter(
        notification_type=Notification.Type.EXPIRY
    ).count() == 1
    send.assert_not_called()


@pytest.mark.django_db
def test_direct_opening_delivery_at_boundary_expires_instead(opening_notification, mocker):
    """Bypassing expiry ordering through direct delivery must not send a late opening email."""
    now = datetime(2026, 8, 21, 0, 0, tzinfo=SHANGHAI)
    opening_notification.task.show_date = date(2026, 8, 20)
    opening_notification.task.save()
    send = mocker.patch("core.services.notifications.send_agent_mail")

    deliver_notification(opening_notification.pk, now=now)

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.last_error == "expired-before-delivery"
    assert opening_notification.task.status == MonitorTask.Status.EXPIRED
    assert opening_notification.task.notifications.filter(
        notification_type=Notification.Type.EXPIRY
    ).count() == 1
    send.assert_not_called()


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
