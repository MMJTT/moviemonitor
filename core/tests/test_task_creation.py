from datetime import date

import pytest
from django.core import signing
from django.urls import reverse
from freezegun import freeze_time

from core.adapters.base import CheckResult, CinemaAvailability, ParsedTarget
from core.models import MonitorTask
from core.services.tasks import PreviewCinema, TaskPreviewPayload, sign_preview, unsign_preview

VALID_URL = "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20"


def preview_payload(cinemas=()):
    return TaskPreviewPayload(
        city_id=10,
        city_name="上海",
        source_url=VALID_URL,
        normalized_url=VALID_URL,
        query_key="maoyan:10:fixture",
        movie_id="1545360",
        movie_name="奥德赛",
        show_date="2026-08-20",
        cinemas=cinemas,
    )


@pytest.mark.django_db
def test_preview_requires_verified_smtp(client):
    response = client.post(reverse("core:task-preview"), {"city_id": "10", "source_url": VALID_URL})

    assert response.status_code == 403


@pytest.mark.django_db
def test_preview_rejects_malformed_bracketed_authority_without_a_server_error(
    client, verified_smtp
):
    """Allowing urlsplit's malformed-IPv6 ValueError to escape must fail this test."""
    client.raise_request_exception = False

    response = client.post(
        reverse("core:task-preview"),
        {
            "city_id": "10",
            "source_url": (
                "https://[www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20"
            ),
        },
    )

    assert response.status_code == 400
    assert "invalid URL" in response.content.decode()


@pytest.mark.django_db
def test_preview_fetches_validated_target_and_signs_server_derived_values(
    client, verified_smtp, mocker
):
    target = ParsedTarget(
        platform="maoyan",
        city_id=10,
        city_name="上海",
        movie_id="1545360",
        show_date=date(2026, 8, 20),
        source_url=VALID_URL,
        normalized_url=VALID_URL,
        query_key="maoyan:10:server-derived",
    )
    result = CheckResult(
        target=target,
        movie_name="服务器电影名",
        valid_page=True,
        cinemas=(
            CinemaAvailability(
                cinema_id="37534",
                name="服务器影院",
                normalized_name="服务器影院",
                bookable=False,
                booking_url="",
            ),
        ),
        content_fingerprint="fixture",
    )
    adapter = mocker.patch("core.views.MaoyanAdapter").return_value
    adapter.validate_target.return_value = target
    adapter.fetch.return_value = result

    response = client.post(reverse("core:task-preview"), {"city_id": "10", "source_url": VALID_URL})

    assert response.status_code == 200
    signed_value = response.context["signed_preview"]
    assert unsign_preview(signed_value) == {
        "city_id": 10,
        "city_name": "上海",
        "source_url": VALID_URL,
        "normalized_url": VALID_URL,
        "query_key": "maoyan:10:server-derived",
        "movie_id": "1545360",
        "movie_name": "服务器电影名",
        "show_date": "2026-08-20",
        "cinemas": [{"id": "37534", "name": "服务器影院"}],
    }
    assert "服务器影院" in response.content.decode()


@pytest.mark.django_db(transaction=True)
def test_confirm_creates_one_manual_cinema_task(client, verified_smtp, mocker):
    check_now = mocker.patch("core.services.tasks.enqueue_immediate_check")

    response = client.post(
        reverse("core:task-confirm"),
        {
            "signed_preview": sign_preview(preview_payload()),
            "manual_cinema_name": "MOViE MOViE 影城（前滩太古里店）",
        },
    )

    assert response.status_code == 302
    task = MonitorTask.objects.get()
    assert task.cinema_id == ""
    assert task.cinema_name == "MOViE MOViE 影城（前滩太古里店）"
    assert task.normalized_cinema_name == "movie movie 影城(前滩太古里店)"
    assert task.next_check_at is not None
    check_now.assert_called_once_with(task.pk)


@pytest.mark.django_db(transaction=True)
def test_confirm_uses_selected_cinema_only_from_signed_preview(client, verified_smtp, mocker):
    check_now = mocker.patch("core.services.tasks.enqueue_immediate_check")
    payload = preview_payload((PreviewCinema(id="37534", name="服务器影院"),))

    response = client.post(
        reverse("core:task-confirm"),
        {"signed_preview": sign_preview(payload), "cinema_id": "37534"},
    )

    assert response.status_code == 302
    task = MonitorTask.objects.get()
    assert task.cinema_id == "37534"
    assert task.cinema_name == "服务器影院"
    assert check_now.call_args.args == (task.pk,)


@pytest.mark.django_db
def test_confirm_rejects_a_tampered_signed_preview(client, verified_smtp):
    signed = sign_preview(preview_payload())
    tampered = f"{signed[:-1]}{'a' if signed[-1] != 'a' else 'b'}"

    response = client.post(
        reverse("core:task-confirm"),
        {"signed_preview": tampered, "manual_cinema_name": "另一影院"},
    )

    assert response.status_code == 400
    assert MonitorTask.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"cinema_id": "37534", "manual_cinema_name": "另一影院"},
        {"cinema_id": "unknown"},
    ],
)
def test_confirm_requires_exactly_one_visible_or_manual_cinema(client, verified_smtp, data):
    payload = preview_payload((PreviewCinema("37534", "服务器影院"),))
    response = client.post(
        reverse("core:task-confirm"),
        {"signed_preview": sign_preview(payload), **data},
    )

    assert response.status_code == 400
    assert MonitorTask.objects.count() == 0


@pytest.mark.django_db
def test_second_unfinished_task_is_rejected(client, verified_smtp, active_task, signed_preview):
    response = client.post(
        reverse("core:task-confirm"),
        {"signed_preview": signed_preview, "manual_cinema_name": "另一影院"},
    )

    assert response.status_code == 400
    assert MonitorTask.objects.count() == 1


def test_preview_signature_expires_after_thirty_minutes():
    signed = sign_preview(preview_payload())

    with pytest.raises(signing.SignatureExpired):
        unsign_preview(signed, max_age=-1)


@pytest.mark.django_db
def test_confirm_rejects_current_date_preview_after_local_midnight(client, verified_smtp):
    """Creating an already-past task from a still-valid signature must fail this test."""
    with freeze_time("2026-08-20 15:50:00"):
        signed = sign_preview(preview_payload())

    with freeze_time("2026-08-20 16:10:00"):
        response = client.post(
            reverse("core:task-confirm"),
            {
                "signed_preview": signed,
                "manual_cinema_name": "MOViE MOViE 影城（前滩太古里店）",
            },
        )

    assert response.status_code == 400
    assert "日期已过" in response.content.decode()
    assert MonitorTask.objects.count() == 0
