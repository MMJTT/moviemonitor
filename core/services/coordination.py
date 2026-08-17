import logging
import threading

import redis
from django.conf import settings
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_WAKE_QUEUE = "ticketwatch:worker:wake"
_fallback_wait_event = threading.Event()


def _redis_client(socket_timeout=1):
    return redis.Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=socket_timeout,
    )


def notify_worker() -> bool:
    if not settings.REDIS_URL:
        return False
    try:
        client = _redis_client()
        client.lpush(_WAKE_QUEUE, "1")
        client.ltrim(_WAKE_QUEUE, 0, 0)
    except RedisError as exc:
        logger.warning("Redis worker notification failed: %s", type(exc).__name__)
        return False
    return True


def wait_for_worker(
    timeout_seconds: int, stop_event: threading.Event | None = None
) -> bool:
    if settings.REDIS_URL:
        try:
            client = _redis_client(socket_timeout=timeout_seconds + 1)
            return client.brpop(_WAKE_QUEUE, timeout=timeout_seconds) is not None
        except RedisError as exc:
            logger.warning("Redis worker wait failed: %s", type(exc).__name__)
    (stop_event or _fallback_wait_event).wait(timeout_seconds)
    return False
