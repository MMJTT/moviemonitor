import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.adapters.base import (
    PageStructureError,
    ParsedTarget,
    RateLimitedError,
    TargetValidationError,
    TemporaryPlatformError,
)
from core.adapters.maoyan import MaoyanAdapter, normalize_cinema_name
from core.models import AgentMailConfig, AppSetting, CheckRun, MonitorTask, Notification
from core.services.agent_mail import AgentMailError, verify_agent_mail
from core.services.leases import StaleTaskClaim, claim_matches
from core.services.scheduling import interval_seconds_for

PREVIEW_SALT = "local-task-preview"
FAILURE_BACKOFF_SECONDS = (120, 300, 900, 1800, 3600)
TASK_NO_LONGER_DETECTED = "task-no-longer-detected"

_FAILURE_DETAILS = {
    TemporaryPlatformError: (
        CheckRun.Status.TEMPORARY_ERROR,
        "platform-temporary",
        "猫眼临时请求失败",
    ),
    RateLimitedError: (
        CheckRun.Status.RATE_LIMITED,
        "platform-rate-limited",
        "猫眼访问受限",
    ),
    PageStructureError: (
        CheckRun.Status.STRUCTURE_ERROR,
        "platform-structure",
        "猫眼页面结构无法验证",
    ),
    TargetValidationError: (
        CheckRun.Status.CONFIG_ERROR,
        "target-config",
        "监控目标配置无效",
    ),
}


class TaskCreationError(ValueError):
    """Raised when a signed preview cannot become a monitoring task."""


class TaskTransitionError(ValueError):
    """Raised when the current durable task state rejects a lifecycle action."""


_task_transition_lock = threading.RLock()


@contextmanager
def task_transition():
    with _task_transition_lock:
        yield


opening_notification_transition = task_transition


@dataclass(frozen=True)
class PreviewCinema:
    id: str
    name: str


@dataclass(frozen=True)
class TaskPreviewPayload:
    city_id: int
    city_name: str
    source_url: str
    normalized_url: str
    query_key: str
    movie_id: str
    movie_name: str
    show_date: str
    cinemas: tuple[PreviewCinema, ...]


def sign_preview(payload: TaskPreviewPayload) -> str:
    return signing.dumps(asdict(payload), salt=PREVIEW_SALT, compress=True)


def unsign_preview(value: str, max_age: int = 30 * 60) -> dict:
    return signing.loads(value, salt=PREVIEW_SALT, max_age=max_age)


def _payload_from_signed_preview(value: str) -> TaskPreviewPayload:
    raw = unsign_preview(value)
    try:
        cinemas = tuple(PreviewCinema(id=item["id"], name=item["name"]) for item in raw["cinemas"])
        payload = TaskPreviewPayload(
            city_id=raw["city_id"],
            city_name=raw["city_name"],
            source_url=raw["source_url"],
            normalized_url=raw["normalized_url"],
            query_key=raw["query_key"],
            movie_id=raw["movie_id"],
            movie_name=raw["movie_name"],
            show_date=raw["show_date"],
            cinemas=cinemas,
        )
        date.fromisoformat(payload.show_date)
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskCreationError("预览数据无效，请重新预览。") from exc
    return payload


def create_task(signed_preview: str, cinema_id: str, manual_name: str) -> MonitorTask:
    config = AgentMailConfig.get_solo()
    if not config.is_verified:
        raise TaskCreationError("请先验证 Agent Mail 配置。")
    try:
        verify_agent_mail()
    except AgentMailError as exc:
        AgentMailConfig.objects.filter(pk=config.pk).update(
            is_verified=False,
            verified_at=None,
            last_error=str(exc),
            updated_at=timezone.now(),
        )
        raise TaskCreationError("Agent Mail 当前无法验证，请到邮件设置重新授权并测试。") from exc

    payload = _payload_from_signed_preview(signed_preview)
    if date.fromisoformat(payload.show_date) < timezone.localdate():
        raise TaskCreationError("预览日期已过，请重新预览。")
    cinema_id = cinema_id.strip()
    manual_name = manual_name.strip()
    if bool(cinema_id) == bool(manual_name):
        raise TaskCreationError("请选择一家可见影院，或手动输入一家影院名称。")

    selected_cinemas = {cinema.id: cinema for cinema in payload.cinemas}
    if cinema_id:
        try:
            selected = selected_cinemas[cinema_id]
        except KeyError as exc:
            raise TaskCreationError("所选影院不在已验证的预览中。") from exc
        cinema_name = selected.name
    else:
        cinema_name = manual_name
        cinema_id = ""

    try:
        with transaction.atomic():
            task = MonitorTask.objects.create(
                source_url=payload.source_url,
                normalized_url=payload.normalized_url,
                query_key=payload.query_key,
                city_id=payload.city_id,
                city_name=payload.city_name,
                movie_id=payload.movie_id,
                movie_name=payload.movie_name,
                show_date=date.fromisoformat(payload.show_date),
                cinema_id=cinema_id,
                cinema_name=cinema_name,
                normalized_cinema_name=normalize_cinema_name(cinema_name),
                next_check_at=timezone.now(),
            )
            transaction.on_commit(lambda: enqueue_immediate_check(task.pk))
    except IntegrityError as exc:
        raise TaskCreationError("已有一个相同目标的未完成任务。") from exc
    return task


def enqueue_immediate_check(task_id) -> None:
    from core.scheduler import wake_scheduler

    wake_scheduler()


def cancel_task(task_id, now=None):
    unfinished_statuses = {
        MonitorTask.Status.MONITORING,
        MonitorTask.Status.PAUSED,
        MonitorTask.Status.DETECTED,
        MonitorTask.Status.ERROR,
    }
    now = now or timezone.now()
    with opening_notification_transition(), transaction.atomic():
        task = MonitorTask.objects.filter(pk=task_id).first()
        if task is None:
            raise MonitorTask.DoesNotExist
        if task.status not in unfinished_statuses:
            raise TaskTransitionError("task state does not allow cancellation")
        opening = (
            Notification.objects.filter(
                task_id=task_id,
                notification_type=Notification.Type.OPENING,
            )
            .order_by("created_at")
            .first()
        )
        if opening is not None and opening.status == Notification.Status.SENDING:
            raise TaskTransitionError("opening notification is already sending")

        task.status = MonitorTask.Status.CANCELLED
        task.cancelled_at = now
        task.next_check_at = None
        task.claim_token = None
        task.claim_expires_at = None
        task.save(
            update_fields=[
                "status",
                "cancelled_at",
                "next_check_at",
                "claim_token",
                "claim_expires_at",
                "updated_at",
            ]
        )
        if opening is not None and opening.status == Notification.Status.PENDING:
            opening.status = Notification.Status.PERMANENT_FAILED
            opening.next_attempt_at = None
            opening.last_error = TASK_NO_LONGER_DETECTED
            opening.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "last_error",
                    "updated_at",
                ]
            )
        return task


def matches_target(task, cinema):
    if task.cinema_id:
        return cinema.cinema_id == task.cinema_id
    return cinema.normalized_name == task.normalized_cinema_name


def next_failure_time(now, configured_seconds, failure_count):
    index = min(failure_count - 1, len(FAILURE_BACKOFF_SECONDS) - 1)
    return now + timedelta(seconds=max(configured_seconds, FAILURE_BACKOFF_SECONDS[index]))


def _not_earlier_than_persisted(persisted, computed):
    if persisted is None:
        return computed
    return max(persisted, computed)


def _target_for_task(task):
    return ParsedTarget(
        platform="maoyan",
        city_id=task.city_id,
        city_name=task.city_name,
        movie_id=task.movie_id,
        show_date=task.show_date,
        source_url=task.source_url,
        normalized_url=task.normalized_url,
        query_key=task.query_key,
    )


def _result_matches_task(result, task):
    target = result.target
    return bool(
        result.valid_page
        and result.movie_name == task.movie_name
        and target.platform == "maoyan"
        and target.city_id == task.city_id
        and target.city_name == task.city_name
        and target.movie_id == task.movie_id
        and target.show_date == task.show_date
        and target.normalized_url == task.normalized_url
        and target.query_key == task.query_key
    )


def _failure_details(exc):
    for error_type, details in _FAILURE_DETAILS.items():
        if isinstance(exc, error_type):
            return details
    raise TypeError("unsupported adapter failure")


def perform_check(task_id, now=None, claim_token=None):
    now = now or timezone.now()
    task = MonitorTask.objects.get(pk=task_id)
    if not claim_matches(task, claim_token):
        raise StaleTaskClaim("task lease no longer belongs to this worker")
    if task.status != MonitorTask.Status.MONITORING:
        previous = task.checks.order_by("-pk").first()
        if previous is None:
            raise MonitorTask.DoesNotExist("task is not available for checking")
        return previous

    target = _target_for_task(task)
    result = None
    failure = None
    try:
        result = MaoyanAdapter().fetch(target)
        if not _result_matches_task(result, task):
            failure = PageStructureError("result context mismatch")
    except tuple(_FAILURE_DETAILS) as exc:
        failure = exc

    finished_at = timezone.now()
    with task_transition(), transaction.atomic():
        current = MonitorTask.objects.select_for_update().get(pk=task_id)
        if not claim_matches(current, claim_token):
            raise StaleTaskClaim("task lease no longer belongs to this worker")
        if current.status != MonitorTask.Status.MONITORING:
            previous = current.checks.order_by("-pk").first()
            if previous is None:
                raise MonitorTask.DoesNotExist("task stopped while its check was running")
            return previous

        if failure is not None:
            check_status, error_code, error_summary = _failure_details(failure)
            check = CheckRun.objects.create(
                task=current,
                status=check_status,
                started_at=now,
                finished_at=finished_at,
                error_code=error_code,
                error_summary=error_summary,
            )
            failure_count = current.consecutive_failures + 1
            current.consecutive_failures = failure_count
            current.last_error = error_summary
            current.last_checked_at = now
            current.claim_token = None
            current.claim_expires_at = None
            if check_status == CheckRun.Status.CONFIG_ERROR or failure_count >= 5:
                current.status = MonitorTask.Status.ERROR
                current.next_check_at = None
            else:
                configured_seconds = interval_seconds_for(
                    current.show_date, now, AppSetting.get_solo()
                )
                current.next_check_at = _not_earlier_than_persisted(
                    current.next_check_at,
                    next_failure_time(now, configured_seconds, failure_count),
                )
            current.save(
                update_fields=[
                    "consecutive_failures",
                    "last_error",
                    "last_checked_at",
                    "next_check_at",
                    "status",
                    "claim_token",
                    "claim_expires_at",
                    "updated_at",
                ]
            )
            return check

        check = CheckRun.objects.create(
            task=current,
            status=CheckRun.Status.SUCCEEDED,
            started_at=now,
            finished_at=finished_at,
            content_fingerprint=result.content_fingerprint,
            cinema_count=len(result.cinemas),
        )
        current.consecutive_failures = 0
        current.last_error = ""
        current.last_checked_at = now
        current.claim_token = None
        current.claim_expires_at = None
        match = next(
            (
                cinema
                for cinema in result.cinemas
                if matches_target(current, cinema) and cinema.bookable and cinema.booking_url
            ),
            None,
        )
        if match is not None:
            current.status = MonitorTask.Status.DETECTED
            current.cinema_id = match.cinema_id
            current.booking_url = match.booking_url
            current.detected_at = now
            current.next_check_at = None
            current.save(
                update_fields=[
                    "status",
                    "cinema_id",
                    "booking_url",
                    "detected_at",
                    "next_check_at",
                    "consecutive_failures",
                    "last_error",
                    "last_checked_at",
                    "claim_token",
                    "claim_expires_at",
                    "updated_at",
                ]
            )
            Notification.objects.get_or_create(
                task=current,
                notification_type=Notification.Type.OPENING,
                defaults={"status": Notification.Status.PENDING},
            )
        else:
            configured_seconds = interval_seconds_for(
                current.show_date, now, AppSetting.get_solo()
            )
            current.next_check_at = _not_earlier_than_persisted(
                current.next_check_at,
                now + timedelta(seconds=configured_seconds),
            )
            current.save(
                update_fields=[
                    "consecutive_failures",
                    "last_error",
                    "last_checked_at",
                    "next_check_at",
                    "claim_token",
                    "claim_expires_at",
                    "updated_at",
                ]
            )
        return check
