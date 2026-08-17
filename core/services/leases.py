import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import MonitorTask


@dataclass(frozen=True)
class TaskClaim:
    task_id: UUID
    token: UUID


class StaleTaskClaim(RuntimeError):
    """Raised when a worker no longer owns the task lease it is submitting."""


def claim_matches(task: MonitorTask, token: UUID | None) -> bool:
    return token is None or task.claim_token == token


def claim_due_task(
    now: datetime | None = None, lease_seconds: int | None = None
) -> TaskClaim | None:
    now = now or timezone.now()
    lease_seconds = (
        settings.WORKER_LEASE_SECONDS if lease_seconds is None else lease_seconds
    )
    with transaction.atomic():
        task = (
            MonitorTask.objects.select_for_update(skip_locked=True)
            .filter(status=MonitorTask.Status.MONITORING, next_check_at__lte=now)
            .filter(Q(claim_expires_at__isnull=True) | Q(claim_expires_at__lte=now))
            .order_by("next_check_at", "created_at", "pk")
            .first()
        )
        if task is None:
            return None
        token = uuid.uuid4()
        task.claim_token = token
        task.claim_expires_at = now + timedelta(seconds=lease_seconds)
        task.save(update_fields=["claim_token", "claim_expires_at", "updated_at"])
        return TaskClaim(task_id=task.pk, token=token)
