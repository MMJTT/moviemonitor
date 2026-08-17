from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from core.models import AppSetting

SHANGHAI = ZoneInfo("Asia/Shanghai")


def interval_seconds_for(show_date: date, now: datetime, setting: AppSetting) -> int:
    local_now = timezone.localtime(now, SHANGHAI)
    target_midnight = datetime.combine(show_date, datetime.min.time(), SHANGHAI)
    remaining_seconds = (target_midnight - local_now).total_seconds()

    if show_date == local_now.date() or remaining_seconds <= setting.urgent_window_hours * 3600:
        return setting.urgent_interval_seconds
    if remaining_seconds <= setting.near_window_days * 24 * 3600:
        return setting.near_interval_seconds
    return setting.far_interval_seconds


def next_check_at_for(show_date: date, now: datetime, setting: AppSetting) -> datetime:
    return now + timedelta(seconds=interval_seconds_for(show_date, now, setting))
