import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from core.models import AppSetting, MonitorTask, Notification, SMTPConfig


@pytest.mark.django_db
def test_poll_interval_cannot_be_below_sixty_seconds():
    setting = AppSetting(poll_interval_seconds=59)

    with pytest.raises(ValidationError):
        setting.full_clean()


@pytest.mark.django_db
def test_app_settings_singleton_returns_the_same_record():
    first = AppSetting.get_solo()
    second = AppSetting.get_solo()

    assert first.pk == second.pk == 1
    assert AppSetting.objects.count() == 1


@pytest.mark.django_db
def test_smtp_config_singleton_returns_the_same_record():
    first = SMTPConfig.get_solo()
    second = SMTPConfig.get_solo()

    assert first.pk == second.pk == 1
    assert SMTPConfig.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_only_one_unfinished_task_is_allowed(task_factory):
    task_factory(status=MonitorTask.Status.MONITORING)

    with pytest.raises(IntegrityError):
        task_factory(status=MonitorTask.Status.PAUSED)


@pytest.mark.django_db(transaction=True)
def test_notification_type_is_unique_per_task(task_factory):
    task = task_factory(status=MonitorTask.Status.CANCELLED)
    Notification.objects.create(task=task, notification_type=Notification.Type.OPENING)

    with pytest.raises(IntegrityError):
        Notification.objects.create(task=task, notification_type=Notification.Type.OPENING)
