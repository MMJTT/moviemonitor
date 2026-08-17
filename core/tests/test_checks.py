import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.adapters.base import (
    CheckResult,
    CinemaAvailability,
    PageStructureError,
    ParsedTarget,
    RateLimitedError,
    TargetValidationError,
    TemporaryPlatformError,
)
from core.adapters.maoyan import MaoyanAdapter, normalize_cinema_name
from core.models import AppSetting, CheckRun, MonitorTask, Notification
from core.services.leases import StaleTaskClaim
from core.services.tasks import perform_check


def result_for(
    task,
    cinema_name,
    bookable,
    *,
    cinema_id="37534",
    valid_page=True,
    city_id=None,
):
    target = ParsedTarget(
        platform="maoyan",
        city_id=task.city_id if city_id is None else city_id,
        city_name=task.city_name,
        movie_id=task.movie_id,
        show_date=task.show_date,
        source_url=task.source_url,
        normalized_url=task.normalized_url,
        query_key=task.query_key,
    )
    return CheckResult(
        target=target,
        movie_name=task.movie_name,
        valid_page=valid_page,
        cinemas=(
            CinemaAvailability(
                cinema_id=cinema_id,
                name=cinema_name,
                normalized_name=normalize_cinema_name(cinema_name),
                bookable=bookable,
                booking_url="https://www.maoyan.com/cinema/37534",
            ),
        ),
        content_fingerprint="fixture-fingerprint",
    )


@pytest.mark.django_db
def test_wrong_claim_token_cannot_submit_check_result(active_task, mocker):
    """Accepting a stale token would let an old worker overwrite newer task state."""
    active_task.claim_token = uuid.uuid4()
    active_task.claim_expires_at = timezone.now() + timedelta(minutes=5)
    active_task.save(update_fields=["claim_token", "claim_expires_at"])
    fetch = mocker.patch("core.services.tasks.MaoyanAdapter.fetch")

    with pytest.raises(StaleTaskClaim):
        perform_check(active_task.pk, claim_token=uuid.uuid4())

    assert not CheckRun.objects.filter(task=active_task).exists()
    fetch.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("outcome", "expected_check_status", "expected_task_status"),
    [
        ("no-opening", CheckRun.Status.SUCCEEDED, MonitorTask.Status.MONITORING),
        ("opening", CheckRun.Status.SUCCEEDED, MonitorTask.Status.DETECTED),
        ("retryable-failure", CheckRun.Status.STRUCTURE_ERROR, MonitorTask.Status.MONITORING),
        ("terminal-failure", CheckRun.Status.STRUCTURE_ERROR, MonitorTask.Status.ERROR),
    ],
)
def test_correct_claim_token_is_cleared_after_check(
    active_task, mocker, outcome, expected_check_status, expected_task_status
):
    """Leaving a completed lease active would block the next due check."""
    token = uuid.uuid4()
    active_task.claim_token = token
    active_task.claim_expires_at = timezone.now() + timedelta(minutes=5)
    if outcome == "terminal-failure":
        active_task.consecutive_failures = 4
        active_task.consecutive_terminal_failures = 4
    active_task.save()
    if outcome.endswith("failure"):
        mocker.patch(
            "core.services.tasks.MaoyanAdapter.fetch",
            side_effect=PageStructureError("untrusted response body"),
        )
    else:
        mocker.patch(
            "core.services.tasks.MaoyanAdapter.fetch",
            return_value=result_for(
                active_task,
                active_task.cinema_name,
                outcome == "opening",
            ),
        )

    check = perform_check(active_task.pk, claim_token=token)

    active_task.refresh_from_db()
    assert check.status == expected_check_status
    assert active_task.status == expected_task_status
    assert active_task.claim_token is None
    assert active_task.claim_expires_at is None


@pytest.mark.django_db
def test_stale_claim_result_cannot_overwrite_pause(client, active_task, mocker):
    """A network result returning after pause must not restore monitoring state."""
    token = uuid.uuid4()
    active_task.claim_token = token
    active_task.claim_expires_at = timezone.now() + timedelta(minutes=5)
    active_task.save(update_fields=["claim_token", "claim_expires_at"])

    def pause_during_fetch(target):
        response = client.post(reverse("core:task-pause", args=[active_task.pk]))
        assert response.status_code == 302
        return result_for(active_task, active_task.cinema_name, True)

    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", side_effect=pause_during_fetch)

    with pytest.raises(StaleTaskClaim):
        perform_check(active_task.pk, claim_token=token)

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.PAUSED
    assert active_task.claim_token is None
    assert active_task.claim_expires_at is None
    assert not CheckRun.objects.filter(task=active_task).exists()


@pytest.mark.django_db
def test_near_name_does_not_detect(active_task, mocker):
    """Replacing full-name equality with fuzzy matching must fail this test."""
    now = timezone.now()
    AppSetting.objects.create(urgent_interval_seconds=180)
    active_task.consecutive_failures = 2
    active_task.consecutive_terminal_failures = 2
    active_task.last_error = "old-local-error"
    active_task.save()
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        return_value=result_for(active_task, "MOViE MOViE 影城（前滩店）", True),
    )

    check = perform_check(active_task.pk, now=now)

    active_task.refresh_from_db()
    assert check.status == CheckRun.Status.SUCCEEDED
    assert check.content_fingerprint == "fixture-fingerprint"
    assert check.cinema_count == 1
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.consecutive_failures == 0
    assert active_task.consecutive_terminal_failures == 0
    assert active_task.last_error == ""
    assert active_task.next_check_at == now + timedelta(seconds=180)
    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_exact_opening_creates_one_notification(active_task, mocker):
    """Creating duplicate opening facts or missing an exact match must fail this test."""
    fetch = mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        return_value=result_for(active_task, active_task.cinema_name, True),
    )

    first = perform_check(active_task.pk)
    second = perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert first.pk == second.pk
    assert fetch.call_count == 1
    assert active_task.status == MonitorTask.Status.DETECTED
    assert active_task.cinema_id == "37534"
    assert active_task.booking_url == "https://www.maoyan.com/cinema/37534"
    assert active_task.detected_at is not None
    assert active_task.next_check_at is None
    assert Notification.objects.filter(
        task=active_task, notification_type=Notification.Type.OPENING
    ).count() == 1


@pytest.mark.django_db
def test_known_cinema_id_takes_precedence_over_name(active_task, mocker):
    """Falling back to a matching name when a known ID differs must fail this test."""
    active_task.cinema_id = "11111"
    active_task.save()
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        return_value=result_for(active_task, active_task.cinema_name, True, cinema_id="37534"),
    )

    perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_later_bookable_duplicate_exact_match_detects_opening(active_task, mocker):
    """Stopping at a closed duplicate before a later bookable exact match must fail this test."""
    active_task.cinema_id = "37534"
    active_task.save()
    target = ParsedTarget(
        platform="maoyan",
        city_id=active_task.city_id,
        city_name=active_task.city_name,
        movie_id=active_task.movie_id,
        show_date=active_task.show_date,
        source_url=active_task.source_url,
        normalized_url=active_task.normalized_url,
        query_key=active_task.query_key,
    )
    result = CheckResult(
        target=target,
        movie_name=active_task.movie_name,
        valid_page=True,
        cinemas=(
            CinemaAvailability(
                cinema_id="37534",
                name=active_task.cinema_name,
                normalized_name=active_task.normalized_cinema_name,
                bookable=False,
                booking_url="",
            ),
            CinemaAvailability(
                cinema_id="37534",
                name=active_task.cinema_name,
                normalized_name=active_task.normalized_cinema_name,
                bookable=True,
                booking_url="https://www.maoyan.com/cinema/37534?bookable=1",
            ),
        ),
        content_fingerprint="duplicate-exact-fixture",
    )
    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", return_value=result)

    perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.DETECTED
    assert active_task.booking_url == "https://www.maoyan.com/cinema/37534?bookable=1"
    assert active_task.notifications.filter(
        notification_type=Notification.Type.OPENING
    ).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("valid_page, city_id", [(False, None), (True, 11)])
def test_unproven_page_context_never_detects(active_task, mocker, valid_page, city_id):
    """Trusting an invalid or mismatched result context must fail this test."""
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        return_value=result_for(
            active_task,
            active_task.cinema_name,
            True,
            valid_page=valid_page,
            city_id=city_id,
        ),
    )

    check = perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert check.status == CheckRun.Status.STRUCTURE_ERROR
    assert active_task.status == MonitorTask.Status.MONITORING
    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_changed_movie_name_never_detects(active_task, mocker):
    """Ignoring a movie-name change after preview must fail this test."""
    result = result_for(active_task, active_task.cinema_name, True)
    result = CheckResult(
        target=result.target,
        movie_name="同一 ID 下的错误电影名",
        valid_page=result.valid_page,
        cinemas=result.cinemas,
        content_fingerprint=result.content_fingerprint,
    )
    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", return_value=result)

    check = perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert check.status == CheckRun.Status.STRUCTURE_ERROR
    assert active_task.status == MonitorTask.Status.MONITORING
    assert not Notification.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (TemporaryPlatformError("secret full response"), "TEMPORARY_ERROR", "platform-temporary"),
        (RateLimitedError("secret full response"), "RATE_LIMITED", "platform-rate-limited"),
        (PageStructureError("secret full response"), "STRUCTURE_ERROR", "platform-structure"),
    ],
)
def test_platform_failures_are_sanitized_and_back_off(
    active_task, mocker, error, expected_status, expected_code
):
    """Leaking platform bodies or polling before the safe backoff must fail this test."""
    now = timezone.now()
    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", side_effect=error)

    check = perform_check(active_task.pk, now=now)

    active_task.refresh_from_db()
    assert check.status == expected_status
    assert check.error_code == expected_code
    assert "secret" not in check.error_summary
    assert "secret" not in active_task.last_error
    assert active_task.consecutive_failures == 1
    assert active_task.next_check_at == now + timedelta(seconds=120)
    assert not Notification.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("malformed_link", ["identity", "booking"])
def test_malformed_bracketed_remote_href_becomes_a_structure_check_failure(
    active_task, mocker, malformed_link
):
    """Letting malformed remote authorities escape the adapter retry policy must fail this test."""
    now = timezone.now()
    identity_href = "/cinema/37534?poi=94710&movieId=1545360"
    booking_href = "/cinema/37534?movieId=1545360"
    if malformed_link == "identity":
        identity_href = "https://[www.maoyan.com/cinema/37534"
    else:
        booking_href = "https://[www.maoyan.com/cinema/37534"
    html = f"""
        <html><body data-city="上海" data-movie-id="1545360" data-movie-name="奥德赛">
          <span class="date-item active" data-date="{active_task.show_date.isoformat()}"></span>
          <div class="cinema-cell">
            <a class="cinema-name" href="{identity_href}">{active_task.cinema_name}</a>
            <span class="buy-btn"><a href="{booking_href}">选座购票</a></span>
          </div>
        </body></html>
    """

    def parse_remote_page(target):
        return MaoyanAdapter().parse_html(html, target)

    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", side_effect=parse_remote_page)

    check = perform_check(active_task.pk, now=now)

    active_task.refresh_from_db()
    assert check.status == CheckRun.Status.STRUCTURE_ERROR
    assert check.error_code == "platform-structure"
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.next_check_at == now + timedelta(seconds=120)
    assert not Notification.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error", "expected_check_status", "expected_category"),
    [
        (
            PageStructureError("untrusted response body"),
            CheckRun.Status.STRUCTURE_ERROR,
            "猫眼页面结构无法验证",
        ),
        (
            TargetValidationError("untrusted target detail"),
            CheckRun.Status.CONFIG_ERROR,
            "监控目标配置无效",
        ),
    ],
)
def test_fifth_structure_or_config_failure_creates_one_system_alert(
    active_task, mocker, error, expected_check_status, expected_category
):
    """Missing or duplicating the terminal alert would hide a stopped task."""
    active_task.consecutive_failures = 4
    active_task.consecutive_terminal_failures = 4
    active_task.save()
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=error,
    )

    first = perform_check(active_task.pk)
    second = perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert first.pk == second.pk
    assert first.status == expected_check_status
    assert active_task.status == MonitorTask.Status.ERROR
    assert active_task.consecutive_failures == 5
    assert active_task.consecutive_terminal_failures == 5
    assert active_task.next_check_at is None
    alert = Notification.objects.get(
        task=active_task, notification_type=Notification.Type.SYSTEM_ALERT
    )
    assert alert.status == Notification.Status.PENDING
    assert active_task.last_error == expected_category


@pytest.mark.django_db
def test_config_error_before_fifth_failure_retries_without_system_alert(
    active_task, mocker
):
    """Stopping on the first deterministic config failure violates the fifth-error threshold."""
    now = timezone.now()
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=TargetValidationError("untrusted target detail"),
    )

    check = perform_check(active_task.pk, now=now)

    active_task.refresh_from_db()
    assert check.status == CheckRun.Status.CONFIG_ERROR
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.consecutive_failures == 1
    assert active_task.consecutive_terminal_failures == 1
    assert active_task.next_check_at == now + timedelta(seconds=120)
    assert not active_task.notifications.filter(
        notification_type=Notification.Type.SYSTEM_ALERT
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error",
    [
        TemporaryPlatformError("private transient response"),
        RateLimitedError("private rate-limit response"),
    ],
)
def test_fifth_temporary_or_rate_failure_keeps_retrying_without_system_alert(
    active_task, mocker, error
):
    """Applying the terminal threshold to retryable failures would stop monitoring."""
    now = timezone.now()
    active_task.consecutive_failures = 4
    active_task.consecutive_terminal_failures = 4
    active_task.save()
    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", side_effect=error)

    perform_check(active_task.pk, now=now)

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.consecutive_failures == 5
    assert active_task.consecutive_terminal_failures == 0
    assert active_task.next_check_at == now + timedelta(seconds=3600)
    assert active_task.claim_token is None
    assert active_task.claim_expires_at is None
    assert not active_task.notifications.filter(
        notification_type=Notification.Type.SYSTEM_ALERT
    ).exists()


@pytest.mark.django_db
def test_resuming_error_task_does_not_create_second_system_alert(
    client, active_task, mocker
):
    """A second terminal episode must preserve the task's single alert fact."""
    active_task.consecutive_failures = 4
    active_task.consecutive_terminal_failures = 4
    active_task.save()
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=PageStructureError("untrusted response body"),
    )
    perform_check(active_task.pk)
    alert = active_task.notifications.get(
        notification_type=Notification.Type.SYSTEM_ALERT
    )
    alert.status = Notification.Status.SENT
    alert.save(update_fields=["status"])

    response = client.post(reverse("core:task-resume", args=[active_task.pk]))
    active_task.refresh_from_db()
    active_task.consecutive_failures = 4
    active_task.consecutive_terminal_failures = 4
    active_task.save()
    perform_check(active_task.pk)

    alert.refresh_from_db()
    assert response.status_code == 302
    assert active_task.notifications.filter(
        notification_type=Notification.Type.SYSTEM_ALERT
    ).count() == 1
    assert alert.status == Notification.Status.SENT


@pytest.mark.django_db
def test_temporary_failures_do_not_advance_terminal_error_streak(active_task, mocker):
    """Four retryable failures must not make the next structure error terminal."""
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=[
            TemporaryPlatformError("private temporary detail"),
            TemporaryPlatformError("private temporary detail"),
            TemporaryPlatformError("private temporary detail"),
            TemporaryPlatformError("private temporary detail"),
            PageStructureError("private structure detail"),
        ],
    )

    for _ in range(5):
        perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.consecutive_failures == 5
    assert active_task.consecutive_terminal_failures == 1
    assert not active_task.notifications.filter(
        notification_type=Notification.Type.SYSTEM_ALERT
    ).exists()


@pytest.mark.django_db
def test_retryable_failure_resets_streak_before_five_mixed_terminal_errors(
    active_task, mocker
):
    """A retryable interruption must restart the five-terminal-error threshold."""
    mocker.patch(
        "core.services.tasks.MaoyanAdapter.fetch",
        side_effect=[
            PageStructureError("private structure detail"),
            PageStructureError("private structure detail"),
            PageStructureError("private structure detail"),
            PageStructureError("private structure detail"),
            TemporaryPlatformError("private temporary detail"),
            PageStructureError("private structure detail"),
            TargetValidationError("private config detail"),
            PageStructureError("private structure detail"),
            TargetValidationError("private config detail"),
            PageStructureError("private structure detail"),
        ],
    )

    for _ in range(5):
        perform_check(active_task.pk)
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.consecutive_failures == 5
    assert active_task.consecutive_terminal_failures == 0

    for _ in range(5):
        perform_check(active_task.pk)

    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.ERROR
    assert active_task.consecutive_failures == 10
    assert active_task.consecutive_terminal_failures == 5
    assert active_task.notifications.filter(
        notification_type=Notification.Type.SYSTEM_ALERT
    ).count() == 1
