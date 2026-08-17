import threading

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from core.services.coordination import notify_worker, wait_for_worker
from core.services.leases import claim_due_task
from core.services.notifications import dispatch_due_notifications, expire_due_task
from core.services.tasks import perform_check


def run_due_work(now=None) -> dict[str, int | bool]:
    now = now or timezone.now()
    expired = expire_due_task(now=now)
    notifications = dispatch_due_notifications(now=now)
    claim = claim_due_task(now=now)
    checked = claim is not None
    if claim is not None:
        perform_check(claim.task_id, now=now, claim_token=claim.token)
    return {"expired": expired, "notifications": notifications, "checked": checked}


class WorkerLoop:
    def __init__(self):
        self._stop_event = threading.Event()

    def run_once(self, now=None) -> dict[str, int | bool]:
        close_old_connections()
        try:
            return run_due_work(now=now)
        finally:
            close_old_connections()

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            result = self.run_once()
            if self._stop_event.is_set():
                break
            if not any(result.values()):
                wait_for_worker(settings.WORKER_SCAN_SECONDS, self._stop_event)

    def stop(self) -> None:
        self._stop_event.set()
        notify_worker()
