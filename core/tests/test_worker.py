import logging
import signal
import threading
import uuid

from django.utils import timezone
from redis.exceptions import ConnectionError

from core.services.coordination import notify_worker, wait_for_worker
from core.services.leases import TaskClaim
from core.worker import WorkerLoop, run_due_work


def test_notify_worker_returns_false_when_redis_is_unconfigured(settings):
    settings.REDIS_URL = ""

    assert notify_worker() is False


def test_notify_worker_pushes_one_lossy_wake_token(settings, mocker):
    settings.REDIS_URL = "redis://example.test:6379/4"
    client = mocker.Mock()
    from_url = mocker.patch(
        "core.services.coordination.redis.Redis.from_url", return_value=client
    )

    assert notify_worker() is True

    from_url.assert_called_once_with(
        settings.REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    client.lpush.assert_called_once_with("ticketwatch:worker:wake", "1")
    client.ltrim.assert_called_once_with("ticketwatch:worker:wake", 0, 0)


def test_notify_worker_logs_only_exception_class_on_redis_failure(
    settings, caplog, mocker
):
    settings.REDIS_URL = "redis://example.test:6379/4"
    mocker.patch(
        "core.services.coordination.redis.Redis.from_url",
        side_effect=ConnectionError("private redis endpoint"),
    )

    with caplog.at_level(logging.WARNING, logger="core.services.coordination"):
        assert notify_worker() is False

    assert "ConnectionError" in caplog.text
    assert "private redis endpoint" not in caplog.text


def test_wait_for_worker_returns_redis_wake_result(settings, mocker):
    settings.REDIS_URL = "redis://example.test:6379/4"
    client = mocker.Mock()
    client.brpop.side_effect = [(b"ticketwatch:worker:wake", b"1"), None]
    mocker.patch("core.services.coordination.redis.Redis.from_url", return_value=client)

    assert wait_for_worker(8) is True
    assert wait_for_worker(8) is False
    assert client.brpop.call_args_list == [
        mocker.call("ticketwatch:worker:wake", timeout=8),
        mocker.call("ticketwatch:worker:wake", timeout=8),
    ]


def test_wait_client_read_timeout_exceeds_blocking_pop_timeout(settings, mocker):
    """A shorter socket timeout would turn a healthy idle Redis into a failure."""
    settings.REDIS_URL = "redis://example.test:6379/4"
    client = mocker.Mock()
    client.brpop.return_value = None
    from_url = mocker.patch(
        "core.services.coordination.redis.Redis.from_url", return_value=client
    )

    assert wait_for_worker(10) is False

    assert from_url.call_args.kwargs["socket_connect_timeout"] == 1
    assert from_url.call_args.kwargs["socket_timeout"] > 10
    client.brpop.assert_called_once_with("ticketwatch:worker:wake", timeout=10)


def test_wait_for_worker_uses_bounded_local_wait_after_redis_failure(
    settings, caplog, mocker
):
    settings.REDIS_URL = "redis://example.test:6379/4"
    stop_event = mocker.Mock(spec=threading.Event)
    mocker.patch(
        "core.services.coordination.redis.Redis.from_url",
        side_effect=ConnectionError("private redis endpoint"),
    )

    with caplog.at_level(logging.WARNING, logger="core.services.coordination"):
        assert wait_for_worker(7, stop_event) is False

    stop_event.wait.assert_called_once_with(7)
    assert "ConnectionError" in caplog.text
    assert "private redis endpoint" not in caplog.text


def test_worker_runs_expiry_mail_then_one_claim(mocker):
    calls = []
    mocker.patch(
        "core.worker.expire_due_task",
        side_effect=lambda now=None: calls.append("expire") or False,
    )
    mocker.patch(
        "core.worker.dispatch_due_notifications",
        side_effect=lambda now=None: calls.append("mail") or 1,
    )
    claim = TaskClaim(task_id=uuid.uuid4(), token=uuid.uuid4())
    mocker.patch(
        "core.worker.claim_due_task",
        side_effect=lambda now=None: calls.append("claim") or claim,
    )
    check = mocker.patch(
        "core.worker.perform_check",
        side_effect=lambda *args, **kwargs: calls.append("check"),
    )

    result = run_due_work(now=timezone.now())

    assert calls == ["expire", "mail", "claim", "check"]
    assert result == {"expired": False, "notifications": 1, "checked": True}
    check.assert_called_once_with(
        claim.task_id, now=mocker.ANY, claim_token=claim.token
    )


def test_worker_loop_skips_wait_while_work_remains(settings, mocker):
    settings.WORKER_SCAN_SECONDS = 23
    loop = WorkerLoop()
    mocker.patch("core.worker.mark_uncertain_sending_notifications", return_value=0)
    run_once = mocker.patch.object(
        loop,
        "run_once",
        side_effect=[
            {"expired": False, "notifications": 1, "checked": False},
            {"expired": False, "notifications": 0, "checked": False},
        ],
    )
    wait = mocker.patch("core.worker.wait_for_worker", side_effect=lambda *args: loop.stop())
    notify = mocker.patch("core.worker.notify_worker", return_value=False)

    loop.run_forever()

    assert run_once.call_count == 2
    wait.assert_called_once_with(23, loop._stop_event)
    notify.assert_called_once_with()


def test_worker_startup_marks_uncertain_notifications_once(settings, mocker):
    """Skipping startup recovery would leave crash-interrupted sends stranded."""
    settings.WORKER_SCAN_SECONDS = 23
    loop = WorkerLoop()
    mark_uncertain = mocker.patch(
        "core.worker.mark_uncertain_sending_notifications", return_value=2
    )
    mocker.patch.object(
        loop,
        "run_once",
        return_value={"expired": False, "notifications": 0, "checked": False},
    )
    mocker.patch("core.worker.wait_for_worker", side_effect=lambda *args: loop.stop())
    mocker.patch("core.worker.notify_worker", return_value=False)

    loop.run_forever()

    mark_uncertain.assert_called_once_with()


def test_worker_stop_interrupts_local_fallback_wait(settings, mocker):
    settings.WORKER_SCAN_SECONDS = 60
    loop = WorkerLoop()
    waiting = threading.Event()
    mocker.patch("core.worker.mark_uncertain_sending_notifications", return_value=0)

    mocker.patch.object(
        loop,
        "run_once",
        return_value={"expired": False, "notifications": 0, "checked": False},
    )

    def local_wait(timeout_seconds, stop_event=None):
        waiting.set()
        stop_event.wait(timeout_seconds)
        return False

    wait = mocker.patch("core.worker.wait_for_worker", side_effect=local_wait)
    notify = mocker.patch("core.worker.notify_worker", return_value=False)
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    assert waiting.wait(timeout=1)

    loop.stop()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    wait.assert_called_once_with(60, loop._stop_event)
    notify.assert_called_once_with()


def test_runworker_registers_stop_only_signal_handlers(mocker):
    from core.management.commands import runworker

    loop = mocker.Mock(spec=WorkerLoop)
    mocker.patch.object(runworker, "WorkerLoop", return_value=loop)
    register = mocker.patch.object(runworker.signal, "signal")

    runworker.Command().handle()

    loop.run_forever.assert_called_once_with()
    handlers = {call.args[0]: call.args[1] for call in register.call_args_list}
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    handlers[signal.SIGINT](signal.SIGINT, None)
    assert loop.stop.call_count == 2
