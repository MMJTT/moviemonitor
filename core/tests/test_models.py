from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from core.models import AgentMailConfig, AppSetting, MonitorTask, Notification


@pytest.mark.django_db
def test_poll_interval_cannot_be_below_sixty_seconds():
    setting = AppSetting(poll_interval_seconds=59)

    with pytest.raises(ValidationError):
        setting.full_clean()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("urgent_window_hours", 0),
        ("near_window_days", 0),
        ("urgent_interval_seconds", 59),
        ("near_interval_seconds", 59),
        ("far_interval_seconds", 59),
    ],
)
def test_adaptive_schedule_values_respect_safety_floors(field, value):
    setting = AppSetting(**{field: value})

    with pytest.raises(ValidationError):
        setting.full_clean()


@pytest.mark.django_db
def test_app_settings_singleton_returns_the_same_record():
    first = AppSetting.get_solo()
    second = AppSetting.get_solo()

    assert first.pk == second.pk == 1
    assert AppSetting.objects.count() == 1


@pytest.mark.django_db
def test_agent_mail_config_singleton_returns_the_same_record():
    first = AgentMailConfig.get_solo()
    second = AgentMailConfig.get_solo()

    assert first.pk == second.pk == 1
    assert AgentMailConfig.objects.count() == 1


@pytest.mark.django_db
def test_agent_mail_recipient_is_fixed_for_server_delivery():
    config = AgentMailConfig.get_solo()

    assert config.recipient_email == "850634546@qq.com"
    assert config._meta.get_field("recipient_email").editable is False


@pytest.mark.django_db
def test_runtime_fields_and_notification_choices_are_available():
    task = MonitorTask()

    assert task.claim_token is None
    assert task.claim_expires_at is None
    assert Notification.Type.SYSTEM_ALERT == "SYSTEM_ALERT"
    assert Notification.Status.NEEDS_REVIEW == "NEEDS_REVIEW"


@pytest.mark.django_db(transaction=True)
def test_multiple_unfinished_tasks_are_allowed(task_factory):
    first = task_factory(status=MonitorTask.Status.MONITORING)
    second = task_factory(
        status=MonitorTask.Status.PAUSED,
        movie_id="2222222",
        movie_name="第二部电影",
        show_date=first.show_date + timedelta(days=1),
        query_key="maoyan:10:second",
    )

    assert MonitorTask.objects.filter(pk__in=[first.pk, second.pk]).count() == 2


@pytest.mark.django_db(transaction=True)
def test_notification_type_is_unique_per_task(task_factory):
    task = task_factory(status=MonitorTask.Status.CANCELLED)
    Notification.objects.create(task=task, notification_type=Notification.Type.OPENING)

    with pytest.raises(IntegrityError):
        Notification.objects.create(task=task, notification_type=Notification.Type.OPENING)
