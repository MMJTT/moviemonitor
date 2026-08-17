import json
from collections import namedtuple
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.utils import timezone
from freezegun import freeze_time
from redis.exceptions import ConnectionError

from core.models import AgentMailConfig, CheckRun, MonitorTask, Notification, RuntimeState
from core.services.runtime_health import collect_runtime_status, record_worker_heartbeat
from core.worker import WorkerLoop

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
pytestmark = pytest.mark.django_db
STATUS_KEYS = {
    "status",
    "database",
    "redis",
    "worker",
    "mail",
    "tasks",
    "last_check_at",
    "backup",
    "disk",
}


@freeze_time("2026-08-17 10:00:00+00:00")
def test_collect_runtime_status_reports_safe_database_backed_summary(
    settings, task_factory, mocker
):
    now = timezone.now()
    monitoring = task_factory()
    task_factory(
        query_key="maoyan:10:error",
        cinema_name="错误影院",
        normalized_cinema_name="错误影院",
        status=MonitorTask.Status.ERROR,
    )
    detected = task_factory(
        query_key="maoyan:10:detected",
        cinema_name="开票影院",
        normalized_cinema_name="开票影院",
        status=MonitorTask.Status.DETECTED,
    )
    Notification.objects.create(
        task=detected,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.PENDING,
    )
    CheckRun.objects.create(
        task=monitoring,
        status=CheckRun.Status.SUCCEEDED,
        started_at=now - timedelta(seconds=2),
        finished_at=now - timedelta(seconds=1),
    )
    RuntimeState.objects.create(
        worker_started_at=now - timedelta(minutes=5),
        worker_heartbeat_at=now - timedelta(seconds=10),
        last_backup_at=now - timedelta(hours=2),
        last_backup_name="ticketwatch-20260817T080000Z.sql.gz",
    )
    mail = AgentMailConfig.get_solo()
    mail.is_verified = True
    mail.verified_at = now - timedelta(days=1)
    mail.save()
    settings.WORKER_SCAN_SECONDS = 10
    settings.REDIS_URL = "redis://user:password@example.test:6379/4?token=private"
    redis_client = mocker.Mock()
    mocker.patch(
        "core.services.runtime_health.redis.Redis.from_url", return_value=redis_client
    )
    mocker.patch(
        "core.services.runtime_health.shutil.disk_usage",
        return_value=DiskUsage(total=1000, used=400, free=600),
    )

    status = collect_runtime_status(now=now)

    assert status.keys() == STATUS_KEYS
    assert status["status"] == "ok"
    assert status["database"] == "ok"
    assert status["redis"] == "ok"
    assert status["worker"] == {
        "status": "ok",
        "started_at": "2026-08-17T17:55:00+08:00",
        "heartbeat_at": "2026-08-17T17:59:50+08:00",
    }
    assert status["mail"] == {
        "status": "ok",
        "verified_at": "2026-08-16T18:00:00+08:00",
    }
    assert status["tasks"] == {
        "monitoring": 1,
        "error": 1,
        "detected": 1,
        "pending_notifications": 1,
    }
    assert status["last_check_at"] == "2026-08-17T17:59:59+08:00"
    assert status["backup"] == {
        "status": "ok",
        "last_at": "2026-08-17T16:00:00+08:00",
        "name": "ticketwatch-20260817T080000Z.sql.gz",
    }
    assert status["disk"] == {
        "status": "ok",
        "total_bytes": 1000,
        "used_bytes": 400,
        "free_bytes": 600,
    }
    serialized = json.dumps(status).lower()
    assert "password" not in serialized
    assert "token" not in serialized
    redis_client.ping.assert_called_once_with()


@pytest.mark.parametrize(
    ("scan_seconds", "heartbeat_age", "expected"),
    [
        (10, timedelta(seconds=30), "ok"),
        (10, timedelta(seconds=31), "stale"),
        (20, timedelta(seconds=60), "ok"),
        (20, timedelta(seconds=61), "stale"),
    ],
)
@freeze_time("2026-08-17 10:00:00+00:00")
def test_worker_status_uses_bounded_scan_interval_staleness(
    scan_seconds, heartbeat_age, expected, settings, mocker
):
    now = timezone.now()
    RuntimeState.objects.create(worker_heartbeat_at=now - heartbeat_age)
    settings.WORKER_SCAN_SECONDS = scan_seconds
    settings.REDIS_URL = "redis://example.test:6379/4"
    mail = AgentMailConfig.get_solo()
    mail.is_verified = True
    mail.verified_at = now
    mail.save()
    mocker.patch("core.services.runtime_health.redis.Redis.from_url")
    mocker.patch(
        "core.services.runtime_health.shutil.disk_usage",
        return_value=DiskUsage(total=1, used=0, free=1),
    )

    assert collect_runtime_status(now=now)["worker"]["status"] == expected


@freeze_time("2026-08-17 10:00:00+00:00")
def test_redis_failure_degrades_status_without_erasing_database_counts(
    settings, task_factory, mocker
):
    now = timezone.now()
    task_factory()
    RuntimeState.objects.create(
        last_backup_at=now,
        last_backup_name="../private/path/ticketwatch-20260817T100000Z.sql.gz",
    )
    settings.REDIS_URL = "redis://private-user:private-password@example.test:6379/4"
    mocker.patch(
        "core.services.runtime_health.redis.Redis.from_url",
        side_effect=ConnectionError("private token and password"),
    )
    mocker.patch(
        "core.services.runtime_health.shutil.disk_usage",
        return_value=DiskUsage(total=1, used=0, free=1),
    )

    status = collect_runtime_status(now=now)

    assert status["status"] == "degraded"
    assert status["database"] == "ok"
    assert status["redis"] == "error"
    assert status["tasks"]["monitoring"] == 1
    assert status["backup"]["name"] == "ticketwatch-20260817T100000Z.sql.gz"
    serialized = json.dumps(status).lower()
    assert "private-password" not in serialized
    assert "private token" not in serialized


def test_malformed_redis_url_returns_safe_degraded_status_with_database_counts(
    settings, task_factory, mocker
):
    task_factory()
    malformed_url = (
        "redis://private-user:private-password@example.test:not-a-port/4?token=private"
    )
    settings.REDIS_URL = malformed_url
    mocker.patch(
        "core.services.runtime_health.shutil.disk_usage",
        return_value=DiskUsage(total=1, used=0, free=1),
    )

    status = collect_runtime_status(now=timezone.now())

    assert status.keys() == STATUS_KEYS
    assert status["status"] == "degraded"
    assert status["database"] == "ok"
    assert status["redis"] == "error"
    assert status["tasks"]["monitoring"] == 1
    serialized = json.dumps(status).lower()
    assert malformed_url.lower() not in serialized
    assert "not-a-port" not in serialized
    assert "private-password" not in serialized
    assert "token" not in serialized


def test_database_failure_returns_controlled_fallback_without_exception_text(
    settings, task_factory, mocker
):
    task_factory()
    settings.REDIS_URL = ""
    mocker.patch(
        "core.services.runtime_health.connection.cursor",
        side_effect=OperationalError(
            "postgresql://private-user:private-password@example.test/db?token=private"
        ),
    )
    mocker.patch(
        "core.services.runtime_health.shutil.disk_usage",
        return_value=DiskUsage(total=1000, used=400, free=600),
    )

    status = collect_runtime_status(now=timezone.now())

    assert status.keys() == STATUS_KEYS
    assert status["status"] == "degraded"
    assert status["database"] == "error"
    assert status["tasks"] == {
        "monitoring": 0,
        "error": 0,
        "detected": 0,
        "pending_notifications": 0,
    }
    assert status["worker"]["status"] == "unavailable"
    assert status["mail"]["status"] == "unavailable"
    assert status["backup"]["status"] == "unavailable"
    serialized = json.dumps(status).lower()
    assert "private-password" not in serialized
    assert "token" not in serialized
    assert "postgresql://" not in serialized


def test_redis_probe_uses_one_second_connect_and_read_timeouts(settings, mocker):
    settings.REDIS_URL = "redis://example.test:6379/4"
    client = mocker.Mock()
    from_url = mocker.patch(
        "core.services.runtime_health.redis.Redis.from_url", return_value=client
    )
    mocker.patch(
        "core.services.runtime_health.shutil.disk_usage",
        return_value=DiskUsage(total=1, used=0, free=1),
    )

    status = collect_runtime_status(now=timezone.now())

    assert status.keys() == STATUS_KEYS
    assert status["redis"] == "ok"
    from_url.assert_called_once_with(
        settings.REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    client.ping.assert_called_once_with()


@freeze_time("2026-08-17 10:00:00+00:00")
def test_record_worker_heartbeat_updates_singleton_state():
    now = timezone.now()

    record_worker_heartbeat(started=True, now=now)

    state = RuntimeState.get_solo()
    assert state.worker_started_at == now
    assert state.worker_heartbeat_at == now
    assert RuntimeState.objects.count() == 1


def test_worker_heartbeat_database_failure_escapes_before_due_work(mocker):
    loop = WorkerLoop()
    due_work = mocker.patch("core.worker.run_due_work")
    mocker.patch(
        "core.worker.record_worker_heartbeat",
        side_effect=OperationalError("database unavailable"),
    )

    with pytest.raises(OperationalError, match="database unavailable"):
        loop.run_once()

    due_work.assert_not_called()


@freeze_time("2026-08-17 10:00:00+00:00")
def test_worker_forever_records_start_and_loop_heartbeat(settings, mocker):
    settings.WORKER_SCAN_SECONDS = 10
    loop = WorkerLoop()
    mocker.patch("core.worker.mark_uncertain_sending_notifications", return_value=0)
    mocker.patch(
        "core.worker.run_due_work",
        return_value={"expired": False, "notifications": 0, "checked": False},
    )
    mocker.patch("core.worker.wait_for_worker", side_effect=lambda *args: loop.stop())
    mocker.patch("core.worker.notify_worker", return_value=False)

    loop.run_forever()

    state = RuntimeState.get_solo()
    assert state.worker_started_at == timezone.now()
    assert state.worker_heartbeat_at == timezone.now()


@freeze_time("2026-08-17 10:00:00+00:00")
def test_mark_backup_success_records_safe_basename():
    call_command(
        "mark_backup_success",
        name="ticketwatch-20260817T020000Z.sql.gz",
    )

    state = RuntimeState.get_solo()
    assert state.last_backup_at == timezone.now()
    assert state.last_backup_name == "ticketwatch-20260817T020000Z.sql.gz"


@pytest.mark.parametrize("name", ["../secret.sql.gz", "nested/secret.sql.gz", "..\\secret"])
def test_mark_backup_success_rejects_path_names(name):
    with pytest.raises(CommandError, match="file name"):
        call_command("mark_backup_success", name=name)


def test_dashboard_displays_runtime_summary(client, mocker):
    mocker.patch(
        "core.views.collect_runtime_status",
        return_value={
            "status": "degraded",
            "database": "ok",
            "redis": "error",
            "worker": {"status": "stale", "started_at": None, "heartbeat_at": None},
            "mail": {"status": "unverified", "verified_at": None},
            "tasks": {
                "monitoring": 2,
                "error": 1,
                "detected": 0,
                "pending_notifications": 3,
            },
            "last_check_at": None,
            "backup": {"status": "missing", "last_at": None, "name": None},
            "disk": {
                "status": "ok",
                "total_bytes": 1000,
                "used_bytes": 400,
                "free_bytes": 600,
            },
        },
    )

    body = client.get("/").content.decode()

    assert "运行状态" in body
    assert "Worker stale" in body
    assert "监控中 2" in body
    assert "待发通知 3" in body
