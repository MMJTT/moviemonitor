from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import AgentMailConfig, MonitorTask, Notification, RuntimeState
from core.services.tasks import PreviewCinema, TaskPreviewPayload, sign_preview


@pytest.fixture
def task_factory(db, owner_user):
    def create(**overrides):
        values = {
            "source_url": "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            "owner": owner_user,
            "normalized_url": "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            "query_key": "maoyan:10:fixture",
            "city_id": 10,
            "city_name": "上海",
            "movie_id": "1545360",
            "movie_name": "奥德赛",
            "show_date": timezone.localdate() + timedelta(days=1),
            "cinema_name": "MOViE MOViE 影城（前滩太古里店）",
            "normalized_cinema_name": "movie movie 影城(前滩太古里店)",
            "status": MonitorTask.Status.MONITORING,
            "next_check_at": timezone.now(),
        }
        values.update(overrides)
        return MonitorTask.objects.create(**values)

    return create


@pytest.fixture
def verified_smtp(db):
    """Compatibility name for tests that only need verified mail delivery."""
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verified_at = timezone.now()
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.save()
    RuntimeState.objects.update_or_create(
        pk=1,
        defaults={"worker_heartbeat_at": timezone.now()},
    )
    return config


@pytest.fixture
def active_task(task_factory):
    return task_factory(status=MonitorTask.Status.MONITORING)


@pytest.fixture
def opening_notification(active_task):
    active_task.status = MonitorTask.Status.DETECTED
    active_task.detected_at = timezone.now()
    active_task.booking_url = "https://www.maoyan.com/cinema/37534"
    active_task.save()
    return Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.PENDING,
    )


@pytest.fixture
def pending_notification(active_task):
    return Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.PENDING,
    )


@pytest.fixture
def signed_preview():
    return sign_preview(
        TaskPreviewPayload(
            city_id=10,
            city_name="上海",
            source_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            normalized_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            query_key="maoyan:10:fixture",
            movie_id="1545360",
            movie_name="奥德赛",
            show_date="2026-08-20",
            cinemas=(PreviewCinema(id="37534", name="MOViE MOViE 影城（前滩太古里店）"),),
        )
    )
