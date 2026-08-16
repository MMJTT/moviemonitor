from datetime import date

import pytest
from freezegun import freeze_time

from core.adapters.base import TargetValidationError
from core.adapters.maoyan import MaoyanAdapter, normalize_cinema_name


@freeze_time("2026-08-16")
def test_valid_target_preserves_source_and_derives_a_stable_normalized_query_key():
    source_url = (
        "https://www.maoyan.com/cinemas?showDate=2026-08-20&brandId=357343&movieId=1545360"
    )

    target = MaoyanAdapter().validate_target(source_url, city_id=10, city_name="上海")

    assert target.source_url == source_url
    assert target.platform == "maoyan"
    assert target.movie_id == "1545360"
    assert target.show_date == date(2026, 8, 20)
    assert target.normalized_url == (
        "https://www.maoyan.com/cinemas?brandId=357343&movieId=1545360&showDate=2026-08-20"
    )
    assert target.query_key == "e90b414348a5a26395823b55e049cf13815ef2d1f55118fc2b6401e5d8408e04"


@pytest.mark.parametrize(
    "url",
    [
        "http://www.maoyan.com/cinemas?movieId=1&showDate=2026-08-20",
        "https://127.0.0.1/cinemas?movieId=1&showDate=2026-08-20",
        "https://example.com/cinemas?movieId=1&showDate=2026-08-20",
        "https://www.maoyan.com/films?movieId=1&showDate=2026-08-20",
        "https://user@www.maoyan.com/cinemas?movieId=1&showDate=2026-08-20",
        "https://www.maoyan.com:444/cinemas?movieId=1&showDate=2026-08-20",
        "https://www.maoyan.com/cinemas?movieId=1&movieId=2&showDate=2026-08-20",
        "https://www.maoyan.com/cinemas?movieId=1&showDate=2026-08-20&unknown=1",
        "https://www.maoyan.com/cinemas?movieId=1&showDate=2026-08-20#fragment",
    ],
)
@freeze_time("2026-08-16")
def test_unsafe_or_ambiguous_target_is_rejected(url):
    with pytest.raises(TargetValidationError):
        MaoyanAdapter().validate_target(url, 10, "上海")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.maoyan.com/cinemas?movieId=movie&showDate=2026-08-20",
        "https://www.maoyan.com/cinemas?movieId=1&showDate=2026-08-15",
        "https://www.maoyan.com/cinemas?movieId=1&showDate=20-08-2026",
    ],
)
@freeze_time("2026-08-16")
def test_target_requires_a_future_or_current_numeric_movie_and_iso_date(url):
    with pytest.raises(TargetValidationError):
        MaoyanAdapter().validate_target(url, 10, "上海")


def test_normalize_cinema_name_keeps_matching_semantics_separate_from_display_name():
    assert normalize_cinema_name("  MOViE  MOViE  影城【前滩太古里店】 ") == (
        "movie movie 影城[前滩太古里店]"
    )
