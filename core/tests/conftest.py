from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import MonitorTask, SMTPConfig
from core.services.tasks import PreviewCinema, TaskPreviewPayload, sign_preview


@pytest.fixture
def task_factory(db):
    def create(**overrides):
        values = {
            "source_url": "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
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
    config = SMTPConfig.get_solo()
    config.host = "smtp.example.com"
    config.username = config.from_email = "sender@example.com"
    config.recipient_email = "receiver@example.com"
    config.encrypted_password = "test-ciphertext"
    config.is_verified = True
    config.save()
    return config


@pytest.fixture
def active_task(task_factory):
    return task_factory(status=MonitorTask.Status.MONITORING)


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
