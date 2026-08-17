import logging
import threading

import pytest
from django.core.management.base import CommandError
from django.utils import timezone

from core.models import MonitorTask, Notification
from core.scheduler import LocalScheduler, set_process_scheduler, wake_scheduler
from core.worker import run_due_work


@pytest.fixture(autouse=True)
def clear_process_scheduler():
    set_process_scheduler(None)
    yield
    set_process_scheduler(None)


@pytest.mark.django_db
def test_due_work_uses_sqlite_due_state_in_safe_order(active_task, pending_notification, mocker):
    """Changing service order or checking a non-due task must fail this test."""
    now = timezone.now()
    active_task.next_check_at = now
    active_task.save(update_fields=["next_check_at"])
    calls = []
    expire = mocker.patch(
        "core.worker.expire_due_task",
        side_effect=lambda now=None: calls.append(("expire", now)) or False,
    )
    deliver = mocker.patch(
        "core.worker.dispatch_due_notifications",
        side_effect=lambda now=None: calls.append(("notifications", now)) or 1,
    )
    check = mocker.patch(
        "core.worker.perform_check",
        side_effect=lambda task_id, now=None, claim_token=None: calls.append(
            ("check", task_id, now)
        ),
    )

    outcome = run_due_work(now=now)

    assert outcome == {"expired": False, "notifications": 1, "checked": True}
    assert calls == [
        ("expire", now),
        ("notifications", now),
        ("check", active_task.pk, now),
    ]
    expire.assert_called_once_with(now=now)
    deliver.assert_called_once_with(now=now)
    check.assert_called_once_with(active_task.pk, now=now, claim_token=mocker.ANY)


@pytest.mark.django_db
def test_due_work_does_not_check_future_or_paused_tasks(task_factory, mocker):
    """Ignoring status or next_check_at would run a check that is not due."""
    now = timezone.now()
    future_task = task_factory(next_check_at=now + timezone.timedelta(minutes=1))
    check = mocker.patch("core.worker.perform_check")
    mocker.patch("core.worker.expire_due_task", return_value=False)
    mocker.patch("core.worker.dispatch_due_notifications", return_value=0)

    assert run_due_work(now=now)["checked"] is False

    future_task.status = MonitorTask.Status.PAUSED
    future_task.next_check_at = now
    future_task.save(update_fields=["status", "next_check_at"])
    assert run_due_work(now=now)["checked"] is False
    check.assert_not_called()


@pytest.mark.django_db
def test_due_work_checks_the_most_overdue_task_first(active_task, task_factory, mocker):
    """Ordering by creation time would let an older task starve a more overdue task."""
    now = timezone.now()
    active_task.next_check_at = now - timezone.timedelta(minutes=1)
    active_task.save(update_fields=["next_check_at"])
    most_overdue = task_factory(
        next_check_at=now - timezone.timedelta(minutes=2),
        movie_id="2222222",
        movie_name="第二部电影",
        query_key="maoyan:10:overdue",
        cinema_name="另一家影院",
        normalized_cinema_name="另一家影院",
    )
    check = mocker.patch("core.worker.perform_check")
    mocker.patch("core.worker.expire_due_task", return_value=False)
    mocker.patch("core.worker.dispatch_due_notifications", return_value=0)

    run_due_work(now=now)

    check.assert_called_once_with(most_overdue.pk, now=now, claim_token=mocker.ANY)


@pytest.mark.django_db
def test_due_work_sends_one_backlogged_notification_then_checks_due_task(
    active_task, task_factory, verified_smtp, mocker
):
    """Draining a mail backlog in one tick would starve checks and multiply SMTP timeouts."""
    now = timezone.now()
    active_task.next_check_at = now
    active_task.save(update_fields=["next_check_at"])
    for _ in range(3):
        terminal_task = task_factory(status=MonitorTask.Status.CANCELLED)
        Notification.objects.create(
            task=terminal_task,
            notification_type=Notification.Type.EXPIRY,
            status=Notification.Status.PENDING,
            next_attempt_at=now,
        )
    send = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )
    check = mocker.patch("core.worker.perform_check")

    outcome = run_due_work(now=now)

    assert outcome == {"expired": False, "notifications": 1, "checked": True}
    assert Notification.objects.filter(status=Notification.Status.SENT).count() == 1
    assert Notification.objects.filter(status=Notification.Status.PENDING).count() == 2
    assert send.call_count == 1
    check.assert_called_once_with(active_task.pk, now=now, claim_token=mocker.ANY)


def test_scheduler_lock_skips_reentrant_tick(mocker):
    """Blocking on a second tick would permit scheduler work to pile up."""
    run = mocker.patch("core.scheduler.run_due_work")
    scheduler = LocalScheduler(interval_seconds=0.01)
    scheduler._check_lock.acquire()
    try:
        assert scheduler.tick() is False
    finally:
        scheduler._check_lock.release()
    run.assert_not_called()


def test_scheduler_wake_interrupts_wait_and_stop_joins_without_sleep(mocker):
    """Using an uninterruptible timer would delay immediate checks and shutdown."""
    first_tick = threading.Event()
    second_tick = threading.Event()
    tick_count = 0

    def tick():
        nonlocal tick_count
        tick_count += 1
        (first_tick if tick_count == 1 else second_tick).set()
        return True

    scheduler = LocalScheduler(interval_seconds=60)
    mocker.patch.object(scheduler, "tick", side_effect=tick)

    scheduler.start()
    assert first_tick.wait(timeout=1)
    scheduler.wake()
    assert second_tick.wait(timeout=1)
    scheduler.stop()
    scheduler.join(timeout=1)

    assert scheduler.is_alive() is False
    assert tick_count == 2


def test_scheduler_logs_only_exception_class(caplog, mocker):
    """Logging an exception message could expose remote response or credential text."""
    scheduler = LocalScheduler(interval_seconds=60)

    def fail_once():
        scheduler.stop()
        raise RuntimeError("private remote response")

    mocker.patch.object(scheduler, "tick", side_effect=fail_once)

    with caplog.at_level(logging.ERROR, logger="core.scheduler"):
        scheduler.start()
        scheduler.join(timeout=1)

    assert scheduler.is_alive() is False
    assert "RuntimeError" in caplog.text
    assert "private remote response" not in caplog.text


def test_wake_scheduler_wakes_local_process_before_notifying_worker(mocker):
    """Skipping either wake path, or reversing them, would delay one deployment mode."""
    calls = []
    scheduler = LocalScheduler()
    wake = mocker.patch.object(scheduler, "wake", side_effect=lambda: calls.append("local"))
    notify = mocker.patch(
        "core.scheduler.notify_worker", side_effect=lambda: calls.append("redis") or True
    )

    wake_scheduler()
    set_process_scheduler(scheduler)
    wake_scheduler()
    set_process_scheduler(None)
    wake_scheduler()

    wake.assert_called_once_with()
    assert notify.call_count == 3
    assert calls == ["redis", "local", "redis", "redis"]


def test_runlocal_forces_loopback_without_reloader_and_cleans_up(mocker):
    """Starting a reloader, a second thread, or leaving the thread alive must fail this test."""
    from core.management.commands import runlocal

    schedulers = []

    class FakeScheduler:
        def __init__(self):
            self.started = False
            self.stopped = False
            self.join_timeout = None
            self.wake_count = 0
            schedulers.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            self.join_timeout = timeout

        def wake(self):
            self.wake_count += 1

    received = {}

    def serve(command, *args, **options):
        received.update(options)
        wake_scheduler()
        return "server-stopped"

    mocker.patch.object(runlocal, "LocalScheduler", FakeScheduler)
    mocker.patch.object(runlocal.RunserverCommand, "handle", autospec=True, side_effect=serve)

    result = runlocal.Command().handle(
        addrport="localhost:8000",
        use_reloader=True,
        use_ipv6=True,
    )

    assert result == "server-stopped"
    assert received["addrport"] == "127.0.0.1:8000"
    assert received["use_reloader"] is False
    assert received["use_ipv6"] is False
    assert len(schedulers) == 1
    assert schedulers[0].started is True
    assert schedulers[0].wake_count == 1
    assert schedulers[0].stopped is True
    assert schedulers[0].join_timeout == 5
    wake_scheduler()
    assert schedulers[0].wake_count == 1


def test_runlocal_clears_process_scheduler_when_thread_start_fails(mocker):
    """Retaining a scheduler that never started would misroute every later wakeup."""
    from core.management.commands import runlocal

    class FailingScheduler:
        def __init__(self):
            self.wake_count = 0

        def start(self):
            raise RuntimeError("thread unavailable")

        def wake(self):
            self.wake_count += 1

    scheduler = FailingScheduler()
    mocker.patch.object(runlocal, "LocalScheduler", return_value=scheduler)

    with pytest.raises(RuntimeError, match="thread unavailable"):
        runlocal.Command().handle(addrport=None, use_reloader=True, use_ipv6=False)

    wake_scheduler()
    assert scheduler.wake_count == 0


@pytest.mark.parametrize("addrport", ["0.0.0.0:8000", "192.168.1.2:8000", "[::]:8000"])
def test_runlocal_refuses_non_loopback_addresses(addrport):
    """Accepting a non-loopback bind would expose the unauthenticated local application."""
    from core.management.commands.runlocal import Command

    with pytest.raises(CommandError, match="127.0.0.1:8000"):
        Command().handle(addrport=addrport, use_reloader=True)
