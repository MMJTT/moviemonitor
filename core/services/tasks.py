from dataclasses import asdict, dataclass
from datetime import date

from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.adapters.maoyan import normalize_cinema_name
from core.models import MonitorTask, SMTPConfig

PREVIEW_SALT = "local-task-preview"


class TaskCreationError(ValueError):
    """Raised when a signed preview cannot become a monitoring task."""


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
    if not SMTPConfig.objects.filter(is_verified=True).exists():
        raise TaskCreationError("请先验证 SMTP 配置。")

    payload = _payload_from_signed_preview(signed_preview)
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
            if MonitorTask.objects.filter(
                status__in=[
                    MonitorTask.Status.MONITORING,
                    MonitorTask.Status.PAUSED,
                    MonitorTask.Status.DETECTED,
                    MonitorTask.Status.ERROR,
                ]
            ).exists():
                raise TaskCreationError("已有一个未完成的监控任务。")
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
        raise TaskCreationError("已有一个未完成的监控任务。") from exc
    return task


def enqueue_immediate_check(task_id) -> None:
    from core.scheduler import wake_scheduler

    wake_scheduler()
