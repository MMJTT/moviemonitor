from pathlib import Path

import pytest
import requests
import responses
from freezegun import freeze_time

from core.adapters.base import (
    CinemaAvailability,
    PageStructureError,
    RateLimitedError,
    TemporaryPlatformError,
)
from core.adapters.maoyan import MaoyanAdapter

FIXTURE_DIR = Path(__file__).parents[2] / "tests" / "fixtures" / "maoyan"


def read_fixture(name):
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
@freeze_time("2026-08-16")
def target():
    return MaoyanAdapter().validate_target(
        "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
        city_id=10,
        city_name="上海",
    )


@pytest.fixture
def adapter():
    return MaoyanAdapter()


def test_open_page_returns_exact_bookable_cinema(adapter, target):
    result = adapter.parse_html(read_fixture("open.html"), target)

    assert result.valid_page is True
    assert result.movie_name == "奥德赛"
    assert result.cinemas == (
        CinemaAvailability(
            cinema_id="37534",
            name="MOViE MOViE 影城（前滩太古里店）",
            normalized_name="movie movie 影城(前滩太古里店)",
            bookable=True,
            booking_url=(
                "https://www.maoyan.com/cinema/37534?movieId=1545360&showDate=2026-08-20"
            ),
        ),
    )
    assert result.content_fingerprint == (
        "27b1bea7a2c1521d555535e81e9014dc2654922b2b2449fe028db05c87081cce"
    )


def test_closed_page_is_valid_and_empty(adapter, target):
    result = adapter.parse_html(read_fixture("closed.html"), target)

    assert result.valid_page is True
    assert result.cinemas == ()


def test_captcha_and_malformed_are_not_closed(adapter, target):
    with pytest.raises(RateLimitedError):
        adapter.parse_html(read_fixture("captcha.html"), target)
    with pytest.raises(PageStructureError):
        adapter.parse_html(read_fixture("malformed.html"), target)


def test_page_with_wrong_city_movie_or_date_is_rejected(adapter, target):
    wrong_context = read_fixture("open.html").replace('data-city="上海"', 'data-city="北京"')

    with pytest.raises(PageStructureError):
        adapter.parse_html(wrong_context, target)


@responses.activate
def test_fetch_sends_the_city_cookie_and_parses_the_validated_url(adapter, target):
    responses.add(responses.GET, target.normalized_url, body=read_fixture("open.html"), status=200)

    result = adapter.fetch(target)

    assert result.cinemas[0].bookable is True
    assert responses.calls[0].request.headers["Cookie"] == "ci=10"


@pytest.mark.parametrize("status", [403, 429])
@responses.activate
def test_fetch_maps_platform_rejection_to_rate_limited(adapter, target, status):
    responses.add(responses.GET, target.normalized_url, status=status)

    with pytest.raises(RateLimitedError):
        adapter.fetch(target)


@responses.activate
def test_fetch_rejects_redirects(adapter, target):
    responses.add(
        responses.GET,
        target.normalized_url,
        status=302,
        headers={"Location": "https://example.com/"},
    )

    with pytest.raises(PageStructureError):
        adapter.fetch(target)


@responses.activate
def test_fetch_maps_server_errors_to_temporary_platform_error(adapter, target):
    responses.add(responses.GET, target.normalized_url, status=503)

    with pytest.raises(TemporaryPlatformError):
        adapter.fetch(target)


@responses.activate
def test_fetch_maps_network_errors_to_temporary_platform_error(adapter, target):
    responses.add(
        responses.GET,
        target.normalized_url,
        body=requests.ConnectionError("connection refused"),
    )

    with pytest.raises(TemporaryPlatformError):
        adapter.fetch(target)
