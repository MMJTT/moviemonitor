import shutil
from datetime import timedelta

import redis
from django.conf import settings
from django.db import DatabaseError, connection
from django.db.models import Count, Max, Q
from django.utils import timezone
from redis.exceptions import RedisError

from core.models import AgentMailConfig, CheckRun, MonitorTask, Notification, RuntimeState


def _iso_datetime(value):
    if value is None:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.isoformat()


def _safe_backup_name(value):
    if not value:
        return None
    basename = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if basename in {"", ".", ".."}:
        return None
    return basename


def record_worker_heartbeat(started=False, now=None) -> None:
    now = now or timezone.now()
    values = {"worker_heartbeat_at": now}
    if started:
        values["worker_started_at"] = now
    RuntimeState.objects.update_or_create(pk=1, defaults=values)


def _worker_status(state, now):
    heartbeat = state.worker_heartbeat_at if state else None
    threshold = timedelta(seconds=max(settings.WORKER_SCAN_SECONDS * 3, 30))
    age = now - heartbeat if heartbeat is not None else None
    is_recent = age is not None and timedelta(0) <= age <= threshold
    return {
        "status": "ok" if is_recent else "stale",
        "started_at": _iso_datetime(state.worker_started_at if state else None),
        "heartbeat_at": _iso_datetime(heartbeat),
    }


def _backup_status(state):
    last_at = state.last_backup_at if state else None
    name = _safe_backup_name(state.last_backup_name if state else None)
    return {
        "status": "ok" if last_at is not None and name is not None else "missing",
        "last_at": _iso_datetime(last_at),
        "name": name,
    }


def _database_status(now):
    fallback = {
        "database": "error",
        "worker": {"status": "unavailable", "started_at": None, "heartbeat_at": None},
        "mail": {"status": "unavailable", "verified_at": None},
        "tasks": {
            "monitoring": 0,
            "error": 0,
            "detected": 0,
            "pending_notifications": 0,
        },
        "last_check_at": None,
        "backup": {"status": "unavailable", "last_at": None, "name": None},
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        state = RuntimeState.objects.filter(pk=1).first()
        task_counts = MonitorTask.objects.aggregate(
            monitoring=Count(
                "pk", filter=Q(status=MonitorTask.Status.MONITORING)
            ),
            error=Count("pk", filter=Q(status=MonitorTask.Status.ERROR)),
            detected=Count("pk", filter=Q(status=MonitorTask.Status.DETECTED)),
        )
        task_counts["pending_notifications"] = Notification.objects.filter(
            status=Notification.Status.PENDING
        ).count()
        last_check_at = CheckRun.objects.aggregate(last=Max("finished_at"))["last"]
        mail_config = AgentMailConfig.objects.filter(pk=1).first()
    except DatabaseError:
        return fallback

    return {
        "database": "ok",
        "worker": _worker_status(state, now),
        "mail": {
            "status": "ok" if mail_config and mail_config.is_verified else "unverified",
            "verified_at": _iso_datetime(
                mail_config.verified_at if mail_config and mail_config.is_verified else None
            ),
        },
        "tasks": task_counts,
        "last_check_at": _iso_datetime(last_check_at),
        "backup": _backup_status(state),
    }


def _redis_status():
    if not settings.REDIS_URL:
        return "unconfigured"
    try:
        client = redis.Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
    except (RedisError, ValueError):
        return "error"
    return "ok"


def _disk_status():
    try:
        usage = shutil.disk_usage(settings.BASE_DIR)
    except OSError:
        return {
            "status": "error",
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
        }
    return {
        "status": "ok",
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def collect_runtime_status(now=None) -> dict:
    now = now or timezone.now()
    database_status = _database_status(now)
    redis_status = _redis_status()
    disk_status = _disk_status()
    component_statuses = (
        database_status["database"],
        redis_status,
        database_status["worker"]["status"],
        database_status["mail"]["status"],
        database_status["backup"]["status"],
        disk_status["status"],
    )
    return {
        "status": "ok" if all(value == "ok" for value in component_statuses) else "degraded",
        "database": database_status["database"],
        "redis": redis_status,
        "worker": database_status["worker"],
        "mail": database_status["mail"],
        "tasks": database_status["tasks"],
        "last_check_at": database_status["last_check_at"],
        "backup": database_status["backup"],
        "disk": disk_status,
    }
