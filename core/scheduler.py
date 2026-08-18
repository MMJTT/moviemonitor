import logging
import threading

from django.db import close_old_connections

from core.services.coordination import notify_worker

logger = logging.getLogger(__name__)

_scheduler = None
_scheduler_lock = threading.Lock()


def run_due_work(*args, **kwargs):
    from core.worker import run_due_work as run_worker_due_work

    return run_worker_due_work(*args, **kwargs)


def set_process_scheduler(scheduler):
    global _scheduler
    with _scheduler_lock:
        _scheduler = scheduler


def wake_scheduler():
    with _scheduler_lock:
        scheduler = _scheduler
    if scheduler is not None:
        scheduler.wake()
    notify_worker()


class LocalScheduler(threading.Thread):
    def __init__(self, interval_seconds=1.0):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        super().__init__(name="ticketwatch-scheduler")
        self.interval_seconds = min(float(interval_seconds), 1.0)
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._check_lock = threading.Lock()

    def wake(self):
        self._wake_event.set()

    def stop(self):
        self._stop_event.set()
        self._wake_event.set()

    def tick(self):
        if not self._check_lock.acquire(blocking=False):
            return False
        close_old_connections()
        try:
            run_due_work()
        finally:
            close_old_connections()
            self._check_lock.release()
        return True

    def run(self):
        try:
            while not self._stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    logger.error("Local scheduler tick failed: %s", type(exc).__name__)
                if self._stop_event.is_set():
                    break
                self._wake_event.wait(timeout=self.interval_seconds)
                self._wake_event.clear()
        finally:
            close_old_connections()
