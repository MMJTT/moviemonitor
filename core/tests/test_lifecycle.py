from datetime import timedelta

import pytest
from django.middleware.csrf import _get_new_csrf_string
from django.urls import reverse
from django.utils import timezone

from core.models import MonitorTask, Notification
from core.scheduler import set_process_scheduler
from core.services.notifications import deliver_notification


class RecordingScheduler:
    def __init__(self):
        self.wake_count = 0

    def wake(self):
        self.wake_count += 1


@pytest.fixture(autouse=True)
def clear_process_scheduler():
    set_process_scheduler(None)
    yield
    set_process_scheduler(None)


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "run-now"])
def test_lifecycle_actions_are_post_only(client, active_task, action):
    """Allowing GET to mutate task state would bypass CSRF-protected intent."""
    response = client.get(reverse(f"core:task-{action}", args=[active_task.pk]))

    assert response.status_code == 405


@pytest.mark.django_db
def test_lifecycle_post_requires_csrf(active_task):
    """Exempting lifecycle routes from CSRF would permit unwanted local state changes."""
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    url = reverse("core:task-pause", args=[active_task.pk])

    assert client.post(url).status_code == 403

    token = _get_new_csrf_string()
    client.cookies["csrftoken"] = token
    assert client.post(url, HTTP_X_CSRFTOKEN=token).status_code == 302


@pytest.mark.django_db
def test_pause_stops_monitoring_and_clears_due_time(client, active_task):
    """Leaving a paused task due would let the scheduler continue checking it."""
    response = client.post(reverse("core:task-pause", args=[active_task.pk]))

    active_task.refresh_from_db()
    assert response.status_code == 302
    assert active_task.status == MonitorTask.Status.PAUSED
    assert active_task.next_check_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("starting_status", [MonitorTask.Status.PAUSED, MonitorTask.Status.ERROR])
def test_resume_resets_failures_schedules_now_and_wakes(
    client, task_factory, starting_status, mocker, django_capture_on_commit_callbacks
):
    """Keeping error state or failing to wake would prevent a resumed task from checking."""
    now = timezone.now()
    task = task_factory(
        status=starting_status,
        consecutive_failures=5,
        last_error="old sanitized error",
        next_check_at=None,
    )
    scheduler = RecordingScheduler()
    set_process_scheduler(scheduler)
    mocker.patch("core.views.timezone.now", return_value=now)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("core:task-resume", args=[task.pk]))

    task.refresh_from_db()
    assert response.status_code == 302
    assert task.status == MonitorTask.Status.MONITORING
    assert task.consecutive_failures == 0
    assert task.last_error == ""
    assert task.next_check_at == now
    assert scheduler.wake_count == 1


@pytest.mark.django_db
def test_run_now_only_reschedules_monitoring_task_and_wakes(
    client, active_task, mocker, django_capture_on_commit_callbacks
):
    """A run-now action that does not persist a due timestamp cannot survive process state."""
    now = timezone.now()
    active_task.next_check_at = now + timedelta(hours=1)
    active_task.save(update_fields=["next_check_at"])
    scheduler = RecordingScheduler()
    set_process_scheduler(scheduler)
    mocker.patch("core.views.timezone.now", return_value=now)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(reverse("core:task-run-now", args=[active_task.pk]))

    active_task.refresh_from_db()
    assert response.status_code == 302
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.next_check_at == now
    assert scheduler.wake_count == 1


@pytest.mark.django_db
def test_cancel_detected_task_invalidates_pending_opening_before_dispatch(
    client, opening_notification, verified_smtp, mocker
):
    """Leaving pending opening mail eligible after cancellation could notify against user intent."""
    now = timezone.now()
    mocker.patch("core.views.timezone.now", return_value=now)
    send = mocker.patch("core.services.notifications.send_message", return_value="accepted")

    response = client.post(
        reverse("core:task-cancel", args=[opening_notification.task_id])
    )

    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert response.status_code == 302
    assert opening_notification.task.status == MonitorTask.Status.CANCELLED
    assert opening_notification.task.cancelled_at == now
    assert opening_notification.task.next_check_at is None
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    assert opening_notification.last_error == "task-no-longer-detected"

    deliver_notification(opening_notification.pk, now=now)
    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    send.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "starting_status",
    [
        MonitorTask.Status.MONITORING,
        MonitorTask.Status.PAUSED,
        MonitorTask.Status.DETECTED,
        MonitorTask.Status.ERROR,
    ],
)
def test_cancel_accepts_every_unfinished_status(client, task_factory, starting_status):
    """Rejecting an unfinished state would leave the user unable to end the sole task."""
    task = task_factory(status=starting_status)

    response = client.post(reverse("core:task-cancel", args=[task.pk]))

    task.refresh_from_db()
    assert response.status_code == 302
    assert task.status == MonitorTask.Status.CANCELLED
    assert task.cancelled_at is not None
    assert task.next_check_at is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("action", "starting_status"),
    [
        ("pause", MonitorTask.Status.PAUSED),
        ("pause", MonitorTask.Status.DETECTED),
        ("resume", MonitorTask.Status.MONITORING),
        ("resume", MonitorTask.Status.DETECTED),
        ("run-now", MonitorTask.Status.PAUSED),
        ("run-now", MonitorTask.Status.ERROR),
        ("cancel", MonitorTask.Status.COMPLETED),
        ("cancel", MonitorTask.Status.EXPIRED),
        ("cancel", MonitorTask.Status.CANCELLED),
    ],
)
def test_invalid_lifecycle_transition_is_rejected(client, task_factory, action, starting_status):
    """Silently applying an invalid transition would corrupt the task state machine."""
    task = task_factory(status=starting_status)

    response = client.post(reverse(f"core:task-{action}", args=[task.pk]))

    task.refresh_from_db()
    assert response.status_code == 409
    assert task.status == starting_status


@pytest.mark.django_db
def test_lifecycle_uses_uuid_lookup_and_returns_not_found(client):
    """Falling back to a non-UUID or non-existent task must not mutate another task."""
    import uuid

    response = client.post(reverse("core:task-cancel", args=[uuid.uuid4()]))

    assert response.status_code == 404
