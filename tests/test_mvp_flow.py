from datetime import timedelta
from pathlib import Path

import pytest
import responses
from django.contrib.staticfiles import finders
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from core.adapters.base import CheckResult, ParsedTarget, TemporaryPlatformError
from core.models import (
    AgentMailConfig,
    AppSetting,
    CheckRun,
    MonitorTask,
    Notification,
    RuntimeState,
)
from core.scheduler import run_due_work, set_process_scheduler
from core.services.mail_verification import process_mail_verification
from core.services.tasks import perform_check

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "maoyan"
VALID_URL = "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20"


class RecordingScheduler:
    def __init__(self):
        self.wake_count = 0

    def wake(self):
        self.wake_count += 1


def closed_result_for(task):
    return CheckResult(
        target=ParsedTarget(
            platform="maoyan",
            city_id=task.city_id,
            city_name=task.city_name,
            movie_id=task.movie_id,
            show_date=task.show_date,
            source_url=task.source_url,
            normalized_url=task.normalized_url,
            query_key=task.query_key,
        ),
        movie_name=task.movie_name,
        valid_page=True,
        cinemas=(),
        content_fingerprint="closed-fixture",
    )


@pytest.fixture
def task_factory(db):
    def create(**overrides):
        values = {
            "source_url": (
                "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20"
            ),
            "normalized_url": (
                "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20"
            ),
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
def active_task(task_factory):
    return task_factory()


@pytest.fixture
def verified_smtp(db):
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


@pytest.fixture(autouse=True)
def clear_process_scheduler():
    set_process_scheduler(None)
    yield
    set_process_scheduler(None)


@pytest.mark.django_db
def test_dashboard_guides_first_run_to_agent_mail(client):
    """Removing the mail prerequisite guidance would strand a first-time user."""
    response = client.get(reverse("core:dashboard"))

    body = response.content.decode()
    assert response.status_code == 200
    assert "先验证 Agent Mail" in body
    assert reverse("core:mail-edit") in body


@pytest.mark.django_db
def test_dashboard_shows_all_three_adaptive_intervals(client):
    setting = AppSetting.get_solo()
    setting.urgent_interval_seconds = 61
    setting.near_interval_seconds = 301
    setting.far_interval_seconds = 901
    setting.save()

    body = client.get(reverse("core:dashboard")).content.decode()

    assert "紧急 61 秒" in body
    assert "临近 301 秒" in body
    assert "远期 901 秒" in body
    assert "{{ setting.poll_interval_seconds }}" not in body


@pytest.mark.django_db
def test_dashboard_guides_verified_user_to_new_task(client, verified_smtp):
    """Hiding the next step after mail verification would break the local setup flow."""
    body = client.get(reverse("core:dashboard")).content.decode()

    assert "创建监控任务" in body
    assert reverse("core:task-preview") in body


@pytest.mark.django_db
def test_unverified_user_opening_new_task_is_guided_to_mail_settings(client):
    """A raw forbidden page on first-run navigation would break the guided setup flow."""
    response = client.get(reverse("core:task-preview"))

    assert response.status_code == 302
    assert response.url == reverse("core:mail-edit")


@pytest.mark.django_db
def test_dashboard_shows_active_task_status_and_allowed_actions(client, active_task):
    """Dropping live state or controls would make the active monitor unmanageable."""
    active_task.last_error = "猫眼页面结构无法验证"
    active_task.save(update_fields=["last_error"])

    body = client.get(reverse("core:dashboard")).content.decode()

    assert active_task.movie_name in body
    assert active_task.cinema_name in body
    assert active_task.get_status_display() in body
    assert "猫眼页面结构无法验证" in body
    assert reverse("core:task-detail", args=[active_task.pk]) in body
    assert reverse("core:task-pause", args=[active_task.pk]) in body
    assert reverse("core:task-run-now", args=[active_task.pk]) in body
    assert reverse("core:task-cancel", args=[active_task.pk]) in body
    assert reverse("core:task-resume", args=[active_task.pk]) not in body


@pytest.mark.django_db
def test_dashboard_shows_every_active_task(client, active_task, task_factory):
    """Returning only the newest task would hide another monitor and its controls."""
    second = task_factory(
        movie_id="2222222",
        movie_name="第二部电影",
        show_date=active_task.show_date + timedelta(days=1),
        query_key="maoyan:10:second",
        cinema_name="另一家影院",
        normalized_cinema_name="另一家影院",
    )

    body = client.get(reverse("core:dashboard")).content.decode()

    for task in (active_task, second):
        assert task.movie_name in body
        assert reverse("core:task-detail", args=[task.pk]) in body
        assert reverse("core:task-run-now", args=[task.pk]) in body


@pytest.mark.django_db
def test_dashboard_keeps_recent_terminal_tasks_below_active_tasks(
    client, active_task, task_factory
):
    """Dropping terminal history would make sent alerts and cancellations hard to audit."""
    completed = task_factory(
        status=MonitorTask.Status.COMPLETED,
        next_check_at=None,
        movie_id="3333333",
        movie_name="已经完成的电影",
        query_key="maoyan:10:completed",
        cinema_name="历史影院",
        normalized_cinema_name="历史影院",
    )

    body = client.get(reverse("core:dashboard")).content.decode()

    assert "活动任务" in body
    assert active_task.movie_name in body
    assert "最近记录" in body
    assert completed.movie_name in body
    assert reverse("core:task-detail", args=[completed.pk]) in body


@pytest.mark.django_db
def test_dashboard_shows_terminal_outcome_and_new_task_action(
    client, verified_smtp, task_factory
):
    """Treating a terminal task as active would prevent the next monitor from being created."""
    task = task_factory(status=MonitorTask.Status.COMPLETED, next_check_at=None)

    body = client.get(reverse("core:dashboard")).content.decode()

    assert task.get_status_display() in body
    assert "创建新的监控任务" in body
    assert reverse("core:task-preview") in body
    assert reverse("core:task-pause", args=[task.pk]) not in body


@pytest.mark.django_db
def test_adaptive_interval_form_rejects_subminute_values(client):
    """Accepting a sub-minute interval would violate the platform safety floor."""
    response = client.post(
        reverse("core:settings"),
        {
            "urgent_window_hours": 72,
            "near_window_days": 10,
            "urgent_interval_seconds": 59,
            "near_interval_seconds": 300,
            "far_interval_seconds": 900,
        },
    )

    assert response.status_code == 200
    assert "60" in response.content.decode()
    assert AppSetting.get_solo().urgent_interval_seconds == 60


@pytest.mark.django_db
def test_saving_adaptive_intervals_reschedules_each_monitoring_tier_and_wakes_scheduler(
    client, active_task, task_factory, mocker, django_capture_on_commit_callbacks
):
    """Saving must assign due times from each task's date band."""
    now = timezone.now()
    active_task.next_check_at = now - timedelta(minutes=1)
    active_task.save(update_fields=["next_check_at"])
    near_task = task_factory(
        movie_id="near-task",
        show_date=(now + timedelta(days=5)).date(),
        query_key="maoyan:10:near-task",
    )
    far_task = task_factory(
        movie_id="far-task",
        show_date=(now + timedelta(days=15)).date(),
        query_key="maoyan:10:far-task",
    )
    scheduler = RecordingScheduler()
    set_process_scheduler(scheduler)
    mocker.patch("core.views.timezone.now", return_value=now)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("core:settings"),
            {
                "urgent_window_hours": 72,
                "near_window_days": 10,
                "urgent_interval_seconds": 60,
                "near_interval_seconds": 300,
                "far_interval_seconds": 900,
            },
        )

    active_task.refresh_from_db()
    near_task.refresh_from_db()
    far_task.refresh_from_db()
    assert response.status_code == 302
    assert response.url == reverse("core:settings")
    setting = AppSetting.get_solo()
    assert setting.urgent_window_hours == 72
    assert setting.near_window_days == 10
    assert active_task.next_check_at == now + timedelta(seconds=60)
    assert near_task.next_check_at == now + timedelta(seconds=300)
    assert far_task.next_check_at == now + timedelta(seconds=900)
    assert scheduler.wake_count == 1


@pytest.mark.django_db
def test_adaptive_interval_form_rejects_overlapping_date_bands(client):
    response = client.post(
        reverse("core:settings"),
        {
            "urgent_window_hours": 72,
            "near_window_days": 3,
            "urgent_interval_seconds": 60,
            "near_interval_seconds": 300,
            "far_interval_seconds": 900,
        },
    )

    assert response.status_code == 200
    assert "临近区间必须大于紧急区间。" in response.content.decode()


@pytest.mark.django_db
def test_settings_saved_during_successful_check_keeps_later_due_time(
    client, active_task, mocker
):
    """A completing check must not overwrite a later schedule saved during its fetch."""
    check_started_at = timezone.now()
    settings_saved_at = check_started_at + timedelta(seconds=30)
    mocker.patch("core.views.timezone.now", return_value=settings_saved_at)

    def save_settings_during_fetch(target):
        response = client.post(
            reverse("core:settings"),
            {
                "urgent_window_hours": 48,
                "near_window_days": 7,
                "urgent_interval_seconds": 300,
                "near_interval_seconds": 300,
                "far_interval_seconds": 900,
            },
        )
        assert response.status_code == 302
        return closed_result_for(active_task)

    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=save_settings_during_fetch,
    )

    perform_check(active_task.pk, now=check_started_at)

    active_task.refresh_from_db()
    assert active_task.next_check_at == settings_saved_at + timedelta(seconds=300)


@pytest.mark.django_db
def test_settings_saved_during_failed_check_keeps_later_due_time_and_backoff(
    client, active_task, mocker
):
    """A retry schedule must preserve a later settings write without losing safe backoff."""
    check_started_at = timezone.now()
    settings_saved_at = check_started_at + timedelta(seconds=90)
    mocker.patch("core.views.timezone.now", return_value=settings_saved_at)

    def save_settings_during_fetch(target):
        response = client.post(
            reverse("core:settings"),
            {
                "urgent_window_hours": 48,
                "near_window_days": 7,
                "urgent_interval_seconds": 60,
                "near_interval_seconds": 300,
                "far_interval_seconds": 900,
            },
        )
        assert response.status_code == 302
        raise TemporaryPlatformError("fixture network failure")

    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=save_settings_during_fetch,
    )

    check = perform_check(active_task.pk, now=check_started_at)

    active_task.refresh_from_db()
    assert check.status == CheckRun.Status.TEMPORARY_ERROR
    assert active_task.next_check_at == settings_saved_at + timedelta(seconds=60)


@pytest.mark.django_db
def test_task_detail_shows_only_twenty_newest_checks_and_notifications(
    client, active_task
):
    """An unbounded or oldest-first history would obscure recent monitor evidence."""
    now = timezone.now()
    for index in range(21):
        CheckRun.objects.create(
            task=active_task,
            status=CheckRun.Status.SUCCEEDED,
            started_at=now + timedelta(seconds=index),
            finished_at=now + timedelta(seconds=index),
            error_summary=f"检查-{index}",
        )
    for index in range(21):
        terminal_task = active_task if index < 2 else None
        if terminal_task is None:
            break
        Notification.objects.create(
            task=terminal_task,
            notification_type=(
                Notification.Type.OPENING if index == 0 else Notification.Type.EXPIRY
            ),
            last_error=f"通知-{index}",
        )

    body = client.get(reverse("core:task-detail", args=[active_task.pk])).content.decode()

    assert "最近检查" in body
    assert "通知记录" in body
    assert "检查-20" in body
    assert "检查-1" in body
    assert "检查-0" not in body
    assert "通知-0" in body
    assert "通知-1" in body


@pytest.mark.django_db
def test_task_detail_never_renders_sensitive_or_full_payload_fields(client, active_task):
    """Rendering storage-only payload fields could disclose transport content."""
    check = CheckRun.objects.create(
        task=active_task,
        status=CheckRun.Status.STRUCTURE_ERROR,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        content_fingerprint="response-fingerprint-private",
        error_summary="安全的检查摘要",
    )
    notification = Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.OPENING,
        transport_response="transport-response-private",
        last_error="安全的通知摘要",
    )

    body = client.get(reverse("core:task-detail", args=[active_task.pk])).content.decode()

    assert check.error_summary in body
    assert notification.last_error in body
    assert check.content_fingerprint not in body
    assert notification.transport_response not in body


@pytest.mark.django_db
def test_task_detail_warns_that_sending_notification_result_is_uncertain(
    client, active_task
):
    """Omitting crash-recovery uncertainty could prompt unsafe manual duplicate delivery."""
    active_task.status = MonitorTask.Status.DETECTED
    active_task.next_check_at = None
    active_task.save(update_fields=["status", "next_check_at"])
    Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.SENDING,
    )

    body = client.get(reverse("core:task-detail", args=[active_task.pk])).content.decode()

    assert "发送结果不确定" in body
    assert "不会自动重发" in body


@pytest.mark.django_db
def test_primary_pages_share_local_navigation_and_stylesheet(client, verified_smtp):
    """Standalone pages without local navigation or styling would fragment the workflow."""
    for route_name in ("core:dashboard", "core:mail-edit", "core:task-preview", "core:settings"):
        body = client.get(reverse(route_name)).content.decode()
        assert reverse("core:dashboard") in body
        assert reverse("core:mail-edit") in body
        assert reverse("core:settings") in body
        assert "/static/css/app.css" in body
        assert "cdn" not in body.casefold()


def test_local_design_assets_are_discoverable_by_django():
    """The local server must be able to serve the design system without a CDN."""
    assert finders.find("css/app.css")
    assert finders.find("favicon.svg")


@pytest.mark.django_db
def test_primary_pages_expose_accessible_navigation_state(client, verified_smtp):
    """Every page needs a skip link and a programmatic current navigation item."""
    expected = {
        "core:dashboard": "仪表盘",
        "core:task-preview": "新建任务",
        "core:mail-edit": "邮件",
        "core:settings": "设置",
    }

    for route_name, label in expected.items():
        body = client.get(reverse(route_name)).content.decode()
        assert 'class="skip-link"' in body
        assert 'id="main-content"' in body
        assert f'aria-current="page">{label}</a>' in body
        assert '/static/favicon.svg' in body


@pytest.mark.django_db(transaction=True)
@freeze_time("2026-08-16 04:00:00")
@responses.activate
def test_mocked_local_flow_sends_one_opening_mail_and_completes(
    client, mocker, django_capture_on_commit_callbacks
):
    """Breaking any closed-loop boundary must stop completion or duplicate the opening mail."""
    test_mail = mocker.patch(
        "core.services.mail_verification.test_agent_mail_config",
        return_value="queued",
    )
    send_mail = mocker.patch(
        "core.services.notifications.send_agent_mail", return_value="queued"
    )
    responses.add(
        responses.GET,
        "https://www.maoyan.com/",
        status=200,
        headers={"Set-Cookie": "uuid=safe-flow-fixture; Path=/"},
    )
    responses.add(
        responses.GET,
        VALID_URL,
        body=(FIXTURE_DIR / "open.html").read_text(),
        status=200,
    )

    mail_page = client.get(reverse("core:mail-edit")).content.decode()
    assert "mijiatong@agent.qq.com" in mail_page
    assert "Agent Mail CLI" in mail_page

    mail_response = client.post(
        reverse("core:mail-test"),
        follow=True,
    )

    config = AgentMailConfig.get_solo()
    assert mail_response.status_code == 200
    assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
    assert config.verification_requested_at is not None

    assert process_mail_verification(now=timezone.now()) is True
    RuntimeState.objects.update_or_create(
        pk=1,
        defaults={"worker_heartbeat_at": timezone.now()},
    )
    config.refresh_from_db()
    assert config.is_verified is True
    assert config.verification_status == AgentMailConfig.VerificationStatus.VERIFIED
    test_mail.assert_called_once()

    preview = client.post(
        reverse("core:task-preview"),
        {"city_id": "10", "source_url": VALID_URL},
    )
    assert preview.status_code == 200
    assert "MOViE MOViE 影城（前滩太古里店）" in preview.content.decode()

    with django_capture_on_commit_callbacks(execute=True):
        confirmation = client.post(
            reverse("core:task-confirm"),
            {
                "signed_preview": preview.context["signed_preview"],
                "cinema_id": "37534",
            },
        )

    task = MonitorTask.objects.get()
    assert confirmation.status_code == 302
    assert confirmation.url == reverse("core:task-detail", args=[task.pk])
    assert task.status == MonitorTask.Status.MONITORING

    now = timezone.now()
    first_pass = run_due_work(now=now)
    task.refresh_from_db()
    assert first_pass["checked"] is True
    assert task.status == MonitorTask.Status.DETECTED
    assert task.notifications.filter(notification_type=Notification.Type.OPENING).count() == 1

    delivery_pass = run_due_work(now=now)
    task.refresh_from_db()
    notification = task.notifications.get(notification_type=Notification.Type.OPENING)
    assert delivery_pass["notifications"] == 1
    assert notification.status == Notification.Status.SENT
    assert task.status == MonitorTask.Status.COMPLETED
    assert send_mail.call_count == 1

    completed_body = client.get(reverse("core:dashboard")).content.decode()
    assert "已完成" in completed_body
    assert task.movie_name in completed_body

    no_duplicate_pass = run_due_work(now=now)
    assert no_duplicate_pass == {
        "expired": False,
        "notifications": 0,
        "checked": False,
    }
    assert send_mail.call_count == 1
    assert task.notifications.filter(notification_type=Notification.Type.OPENING).count() == 1
