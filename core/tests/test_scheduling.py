from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from core.models import AppSetting
from core.services.scheduling import interval_seconds_for, next_check_at_for

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("show_date", "expected"),
    [(date(2026, 8, 18), 60), (date(2026, 8, 21), 300), (date(2026, 8, 30), 900)],
)
def test_interval_uses_editable_date_bands(show_date, expected):
    setting = AppSetting.get_solo()
    now = datetime(2026, 8, 17, 12, 0, tzinfo=SHANGHAI)

    assert interval_seconds_for(show_date, now, setting) == expected


@pytest.mark.django_db
def test_show_date_itself_stays_urgent():
    setting = AppSetting.get_solo()
    now = datetime(2026, 8, 20, 23, 59, tzinfo=SHANGHAI)

    assert interval_seconds_for(date(2026, 8, 20), now, setting) == 60


@pytest.mark.django_db
def test_activated_utc_timezone_does_not_change_shanghai_date_band():
    setting = AppSetting.get_solo()
    setting.urgent_window_hours = 1
    setting.near_window_days = 7
    setting.save()
    now = datetime(2026, 8, 17, 16, 30, tzinfo=ZoneInfo("UTC"))

    with timezone.override("UTC"):
        assert interval_seconds_for(date(2026, 8, 18), now, setting) == 60


@pytest.mark.django_db
def test_next_check_uses_the_calculated_interval():
    setting = AppSetting.get_solo()
    now = datetime(2026, 8, 17, 12, 0, tzinfo=SHANGHAI)

    assert next_check_at_for(date(2026, 8, 21), now, setting) == datetime(
        2026, 8, 17, 12, 5, tzinfo=SHANGHAI
    )
