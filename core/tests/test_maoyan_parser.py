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


def test_live_page_structure_returns_normalized_bookable_cinema(adapter, target):
    """Removing live selectors, safe query filtering, or date insertion must fail this test."""
    result = adapter.parse_html(read_fixture("live_open.html"), target)

    assert result.movie_name == "奥德赛"
    assert result.cinemas == (
        CinemaAvailability(
            cinema_id="37534",
            name="MOViE MOViE 影城（前滩太古里店）",
            normalized_name="movie movie 影城(前滩太古里店)",
            bookable=True,
            booking_url=(
                "https://www.maoyan.com/cinema/37534?"
                "movieId=1545360&poi=94710&showDate=2026-08-20"
            ),
        ),
    )


def test_closed_page_is_valid_and_empty(adapter, target):
    result = adapter.parse_html(read_fixture("closed.html"), target)

    assert result.valid_page is True
    assert result.cinemas == ()


def test_live_empty_list_ignores_bookable_cells_outside_the_authoritative_list(adapter, target):
    """Broad cinema selectors that turn page chrome into a detection must fail this test."""
    result = adapter.parse_html(read_fixture("live_closed.html"), target)

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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("currentcityid:10", "currentcityid:11"),
        ('class="city-selected">上海', 'class="city-selected">北京'),
        ("movieid:1545360", "movieid:9999999"),
        ("TagName:'2026-08-20'", "TagName:'2026-08-21'"),
    ],
)
def test_live_page_with_mismatched_authoritative_context_is_rejected(adapter, target, old, new):
    """Trusting any mismatched live city/movie/date marker must fail this test."""
    html = read_fixture("live_open.html").replace(old, new, 1)

    with pytest.raises(PageStructureError):
        adapter.parse_html(html, target)


def test_live_page_requires_one_nonempty_movie_name(adapter, target):
    """Accepting a missing or ambiguous authoritative movie name must fail this test."""
    fixture = read_fixture("live_open.html")
    missing = fixture.replace("<h1 class=\"name\">奥德赛</h1>", "<h1 class=\"name\"> </h1>")
    duplicate = fixture.replace(
        "<h1 class=\"name\">奥德赛</h1>",
        "<h1 class=\"name\">奥德赛</h1><h1 class=\"name\">另一电影</h1>",
    )

    with pytest.raises(PageStructureError):
        adapter.parse_html(missing, target)
    with pytest.raises(PageStructureError):
        adapter.parse_html(duplicate, target)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "/cinema/37534?poi=94710&amp;movieId=1545360&amp;tracking=discarded",
            "/cinema/37534?poi=94710&amp;movieId=9999999",
        ),
        (
            "/cinema/37534?poi=94710&amp;movieId=1545360&amp;tracking=discarded",
            "/cinema/37534?poi=94710&amp;movieId=1545360&amp;showDate=2026-08-21",
        ),
        (
            "/cinema/37534?poi=94710&amp;movieId=1545360&amp;tracking=discarded",
            "https://example.com/cinema/37534?poi=94710&amp;movieId=1545360",
        ),
    ],
)
def test_visible_booking_control_with_mismatched_url_is_a_structure_error(
    adapter, target, old, new
):
    """Downgrading a malformed visible booking control to closed must fail this test."""
    html = read_fixture("live_open.html").replace(old, new, 1)

    with pytest.raises(PageStructureError):
        adapter.parse_html(html, target)


def test_anchored_booking_control_with_changed_label_is_a_structure_error(adapter, target):
    """Silently treating an unknown anchored CTA as closed must fail this test."""
    html = read_fixture("live_open.html").replace(">选座购票</a>", ">立即购票</a>", 1)

    with pytest.raises(PageStructureError):
        adapter.parse_html(html, target)


def test_booking_control_without_an_anchor_is_legitimately_closed(adapter, target):
    """Treating a no-anchor closed-state control as malformed must fail this test."""
    html = read_fixture("live_open.html").replace(
        (
            '<span class="buy-btn">\n'
            '        <a href="/cinema/37534?poi=94710&amp;movieId=1545360&amp;'
            'tracking=discarded">选座购票</a>\n'
            "      </span>"
        ),
        '<span class="buy-btn">暂无场次</span>',
    )

    result = adapter.parse_html(html, target)

    assert result.cinemas[0].bookable is False
    assert result.cinemas[0].booking_url == ""


def test_known_booking_label_without_an_anchor_is_a_structure_error(adapter, target):
    """Silently closing a visible known CTA without an href must fail this test."""
    html = read_fixture("live_open.html").replace(
        (
            '<span class="buy-btn">\n'
            '        <a href="/cinema/37534?poi=94710&amp;movieId=1545360&amp;'
            'tracking=discarded">选座购票</a>\n'
            "      </span>"
        ),
        '<span class="buy-btn">选座购票</span>',
    )

    with pytest.raises(PageStructureError):
        adapter.parse_html(html, target)


@pytest.mark.parametrize(
    "identity_href",
    [
        "https://example.com/cinema/37534?poi=94710&movieId=1545360",
        "//example.com/cinema/37534?poi=94710&movieId=1545360",
        "http://www.maoyan.com/cinema/37534?poi=94710&movieId=1545360",
        "https://user@www.maoyan.com/cinema/37534?poi=94710&movieId=1545360",
        "https://www.maoyan.com:444/cinema/37534?poi=94710&movieId=1545360",
        "/cinema/37534?poi=94710&movieId=1545360#fragment",
        "/cinema/37534?poi=94710&movieId=1545360&tracking=unexpected",
        "/cinema/37534?poi=94710&movieId=1545360&movieId=1545360",
        "/cinema/37534?poi=94710&movieId=9999999",
        "/cinema/37534?movieId=1545360",
        "/cinema/37534?poi=not-a-number&movieId=1545360",
    ],
)
def test_live_cinema_identity_rejects_unsafe_or_conflicting_urls(
    adapter, target, identity_href
):
    """Accepting an unsafe identity URL because its path looks valid must fail this test."""
    html = read_fixture("live_open.html").replace(
        "/cinema/37534?poi=94710&amp;movieId=1545360",
        identity_href.replace("&", "&amp;"),
        1,
    )

    with pytest.raises(PageStructureError):
        adapter.parse_html(html, target)


@pytest.mark.parametrize(
    "identity_href",
    [
        "/cinema/37534?poi=94710",
        "/cinema/37534?poi=94710&movieId=1545360",
    ],
)
def test_live_cinema_identity_accepts_safe_observed_query_variants(
    adapter, target, identity_href
):
    """Requiring movieId when a numeric poi already identifies the cinema must fail this test."""
    html = read_fixture("live_open.html").replace(
        "/cinema/37534?poi=94710&amp;movieId=1545360",
        identity_href.replace("&", "&amp;"),
        1,
    )

    result = adapter.parse_html(html, target)

    assert result.cinemas[0].cinema_id == "37534"


@pytest.mark.parametrize("host", ["maoyan.com", "www.maoyan.com"])
def test_live_cinema_identity_accepts_https_maoyan_hosts(adapter, target, host):
    """Rejecting a permitted absolute Maoyan identity URL must fail this test."""
    html = read_fixture("live_open.html").replace(
        "/cinema/37534?poi=94710&amp;movieId=1545360",
        f"https://{host}/cinema/37534?poi=94710&amp;movieId=1545360",
        1,
    )

    result = adapter.parse_html(html, target)

    assert result.cinemas[0].cinema_id == "37534"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            (
                '<a class="cinema-name" '
                'href="/cinema/37534?poi=94710&amp;movieId=1545360">'
            ),
            (
                '<a class="cinema-name" '
                'href="/cinema/37534?poi=94710&amp;movieId=1545360">副名称</a>'
                '<a class="cinema-name" '
                'href="/cinema/37534?poi=94710&amp;movieId=1545360">'
            ),
        ),
        (
            '<a href="/cinema/37534?poi=94710&amp;movieId=1545360&amp;tracking=discarded">',
            "<a>",
        ),
        (
            "</a>\n      </span>\n    </div>",
            (
                "</a>"
                '<a href="/cinema/37534?movieId=1545360">选座购票</a>'
                "\n      </span>\n    </div>"
            ),
        ),
    ],
)
def test_live_cinema_cell_rejects_ambiguous_identity_or_booking_control(
    adapter, target, old, new
):
    """Accepting ambiguous identity or booking controls must fail this test."""
    html = read_fixture("live_open.html").replace(old, new, 1)

    with pytest.raises(PageStructureError):
        adapter.parse_html(html, target)


def _add_bootstrap_response(*, status=200, body="", headers=None):
    responses.add(
        responses.GET,
        "https://www.maoyan.com/",
        body=body,
        status=status,
        headers=headers,
    )


@responses.activate
def test_fetch_bootstraps_session_then_sends_city_cookie_to_validated_target(adapter, target):
    """Skipping bootstrap or leaking the city cookie into it must fail this test."""
    _add_bootstrap_response(headers={"Set-Cookie": "uuid=safe-fixture-cookie; Path=/"})
    responses.add(responses.GET, target.normalized_url, body=read_fixture("open.html"), status=200)

    result = adapter.fetch(target)

    assert result.cinemas[0].bookable is True
    assert len(responses.calls) == 2
    assert "Cookie" not in responses.calls[0].request.headers
    assert responses.calls[0].request.headers["User-Agent"]
    assert responses.calls[1].request.headers["Cookie"] == "uuid=safe-fixture-cookie; ci=10"


@pytest.mark.parametrize("stage", ["bootstrap", "target"])
@responses.activate
def test_fetch_rejects_redirects_without_following_location(adapter, target, stage):
    """Following a redirect at either request boundary must fail this test."""
    if stage == "bootstrap":
        _add_bootstrap_response(status=302, headers={"Location": "https://example.com/"})
    else:
        _add_bootstrap_response()
        responses.add(
            responses.GET,
            target.normalized_url,
            status=302,
            headers={"Location": "https://example.com/"},
        )

    with pytest.raises(PageStructureError):
        adapter.fetch(target)

    assert all(call.request.url != "https://example.com/" for call in responses.calls)


@pytest.mark.parametrize("stage", ["bootstrap", "target"])
@pytest.mark.parametrize("status", [403, 429])
@responses.activate
def test_fetch_maps_platform_rejection_to_rate_limited(adapter, target, stage, status):
    """Treating a bootstrap or target rejection as an empty list must fail this test."""
    if stage == "bootstrap":
        _add_bootstrap_response(status=status)
    else:
        _add_bootstrap_response()
        responses.add(responses.GET, target.normalized_url, status=status)

    with pytest.raises(RateLimitedError):
        adapter.fetch(target)


@pytest.mark.parametrize("stage", ["bootstrap", "target"])
@responses.activate
def test_fetch_maps_server_errors_to_temporary_platform_error(adapter, target, stage):
    """Treating a bootstrap or target server error as valid content must fail this test."""
    if stage == "bootstrap":
        _add_bootstrap_response(status=503)
    else:
        _add_bootstrap_response()
        responses.add(responses.GET, target.normalized_url, status=503)

    with pytest.raises(TemporaryPlatformError):
        adapter.fetch(target)


@pytest.mark.parametrize("stage", ["bootstrap", "target"])
@responses.activate
def test_fetch_maps_network_errors_to_temporary_platform_error(adapter, target, stage):
    """Leaking request exceptions from either network boundary must fail this test."""
    if stage == "bootstrap":
        _add_bootstrap_response(body=requests.ConnectionError("connection refused"))
    else:
        _add_bootstrap_response()
        responses.add(
            responses.GET,
            target.normalized_url,
            body=requests.ConnectionError("connection refused"),
        )

    with pytest.raises(TemporaryPlatformError):
        adapter.fetch(target)
