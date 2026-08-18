import threading
from contextlib import nullcontext

import pytest
from django.db import close_old_connections, connection, connections
from django.test import Client
from django.urls import reverse

from core.models import MonitorTask, Notification
from core.services.notifications import deliver_notification

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL"),
    pytest.mark.django_db(transaction=True),
]


def _database_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _start_database_thread(name, target, backend_pids):
    errors = []

    def run():
        close_old_connections()
        try:
            backend_pids.append(_database_pid())
            target()
        except BaseException as error:
            errors.append(error)
        finally:
            connections.close_all()

    thread = threading.Thread(name=name, target=run)
    thread.start()
    return thread, errors


def _disable_process_local_transition_locks(mocker):
    mocker.patch(
        "core.services.tasks.opening_notification_transition",
        side_effect=nullcontext,
        create=True,
    )
    mocker.patch(
        "core.services.notifications.opening_notification_transition",
        side_effect=nullcontext,
        create=True,
    )


def _post_cancel(task_id, owner_user):
    client = Client()
    client.force_login(owner_user)
    return client.post(reverse("core:task-cancel", args=[task_id]))


def test_postgres_cancel_first_prevents_delivery_without_stale_overwrite(
    opening_notification, verified_smtp, mocker, owner_user
):
    """A process-local lock cannot protect Web cancellation from Worker delivery."""
    _disable_process_local_transition_locks(mocker)
    cancel_before_write = threading.Event()
    release_cancel = threading.Event()
    original_task_save = MonitorTask.save

    def coordinated_task_save(task, *args, **kwargs):
        if (
            threading.current_thread().name == "postgres-cancel"
            and task.status == MonitorTask.Status.CANCELLED
        ):
            cancel_before_write.set()
            assert release_cancel.wait(timeout=5)
        return original_task_save(task, *args, **kwargs)

    mocker.patch.object(MonitorTask, "save", new=coordinated_task_save)
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )
    responses = []
    backend_pids = []
    cancel_thread, cancel_errors = _start_database_thread(
        "postgres-cancel",
        lambda: responses.append(
            _post_cancel(opening_notification.task_id, owner_user)
        ),
        backend_pids,
    )
    assert cancel_before_write.wait(timeout=5)

    delivery_thread, delivery_errors = _start_database_thread(
        "postgres-delivery",
        lambda: deliver_notification(opening_notification.pk),
        backend_pids,
    )
    delivery_thread.join(timeout=0.5)
    release_cancel.set()
    cancel_thread.join(timeout=5)
    delivery_thread.join(timeout=5)

    assert cancel_thread.is_alive() is False
    assert delivery_thread.is_alive() is False
    assert cancel_errors == []
    assert delivery_errors == []
    assert len(set(backend_pids)) == 2
    assert len(responses) == 1
    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    if responses[0].status_code == 302:
        send.assert_not_called()
        assert opening_notification.task.status == MonitorTask.Status.CANCELLED
        assert opening_notification.status == Notification.Status.PERMANENT_FAILED
    else:
        assert responses[0].status_code == 409
        send.assert_called_once()
        assert opening_notification.task.status == MonitorTask.Status.COMPLETED
        assert opening_notification.status == Notification.Status.SENT


def test_postgres_delivery_first_completes_and_cancel_returns_conflict(
    opening_notification, verified_smtp, mocker, owner_user
):
    """Delivery ownership must be visible to an independent cancelling connection."""
    _disable_process_local_transition_locks(mocker)
    send_started = threading.Event()
    release_send = threading.Event()

    def blocked_send(recipient, subject, body):
        send_started.set()
        assert release_send.wait(timeout=5)
        return "queued"

    mocker.patch(
        "core.services.notifications.send_agent_mail", side_effect=blocked_send
    )
    backend_pids = []
    delivery_thread, delivery_errors = _start_database_thread(
        "postgres-delivery",
        lambda: deliver_notification(opening_notification.pk),
        backend_pids,
    )
    assert send_started.wait(timeout=5)
    cancelling_pid = _database_pid()

    response = _post_cancel(opening_notification.task_id, owner_user)
    release_send.set()
    delivery_thread.join(timeout=5)

    assert delivery_thread.is_alive() is False
    assert delivery_errors == []
    assert backend_pids[0] != cancelling_pid
    assert response.status_code == 409
    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED
    assert opening_notification.task.cancelled_at is None
