import hashlib
import unicodedata
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
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

MAOYAN_HOSTS = frozenset({"maoyan.com", "www.maoyan.com"})
ALLOWED_FILTERS = frozenset(
    {"movieId", "showDate", "brandId", "districtId", "hallType", "serviceId"}
)
BASE_URL = "https://www.maoyan.com"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


def normalize_cinema_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"（": "(", "）": ")", "【": "[", "】": "]"})
    ).casefold()
    return " ".join(value.split())


class MaoyanAdapter:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def validate_target(self, url: str, city_id: int, city_name: str) -> ParsedTarget:
        parsed = urlsplit(url)
        self._validate_url_parts(parsed)
        parameters = parse_qsl(parsed.query, keep_blank_values=True)
        self._validate_parameters(parameters)
        values = dict(parameters)
        movie_id = values["movieId"]
        show_date = self._parse_show_date(values["showDate"])
        normalized_url = urlunsplit(
            (
                "https",
                parsed.hostname.lower(),
                "/cinemas",
                urlencode(sorted(parameters)),
                "",
            )
        )
        query_key = hashlib.sha256(
            f"maoyan:{city_id}:{normalized_url}".encode()
        ).hexdigest()
        return ParsedTarget(
            platform="maoyan",
            city_id=city_id,
            city_name=city_name,
            movie_id=movie_id,
            show_date=show_date,
            source_url=url,
            normalized_url=normalized_url,
            query_key=query_key,
        )

    def fetch(self, target: ParsedTarget) -> CheckResult:
        try:
            response = self.session.get(
                target.normalized_url,
                headers={"User-Agent": DESKTOP_USER_AGENT},
                cookies={"ci": str(target.city_id)},
                timeout=15,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise TemporaryPlatformError("network request failed") from exc

        if 300 <= response.status_code < 400:
            raise PageStructureError("redirected platform response")
        if response.status_code in {403, 429}:
            raise RateLimitedError("platform request rejected")
        if 500 <= response.status_code < 600:
            raise TemporaryPlatformError("platform server error")
        if response.status_code != 200:
            raise TemporaryPlatformError("unexpected platform status")
        return self.parse_html(response.text, target)

    def parse_html(self, html: str, target: ParsedTarget) -> CheckResult:
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one("#verify") is not None:
            raise RateLimitedError("platform verification required")

        body = soup.body
        if body is None:
            raise PageStructureError("response has no page body")
        if body.get("data-city") != target.city_name:
            raise PageStructureError("response city does not match target")
        if body.get("data-movie-id") != target.movie_id:
            raise PageStructureError("response movie does not match target")
        movie_name = body.get("data-movie-name", "").strip()
        if not movie_name:
            raise PageStructureError("response movie name is missing")

        active_dates = soup.select(".date-item.active[data-date]")
        if len(active_dates) != 1 or active_dates[0]["data-date"] != target.show_date.isoformat():
            raise PageStructureError("response date does not match target")

        cinema_cells = soup.select(".cinema-cell")
        no_cinemas = soup.select_one(".no-cinemas")
        if cinema_cells and no_cinemas:
            raise PageStructureError("response has conflicting list structures")
        if no_cinemas is not None:
            cinemas: tuple[CinemaAvailability, ...] = ()
        elif cinema_cells:
            cinemas = tuple(self._parse_cinema_cell(cell, target) for cell in cinema_cells)
        else:
            raise PageStructureError("response has no cinema list structure")

        return CheckResult(
            target=target,
            movie_name=movie_name,
            valid_page=True,
            cinemas=cinemas,
            content_fingerprint=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _validate_url_parts(parsed) -> None:
        try:
            port = parsed.port
        except ValueError as exc:
            raise TargetValidationError("invalid URL port") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in MAOYAN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.path != "/cinemas"
            or parsed.fragment
        ):
            raise TargetValidationError("URL must be a direct Maoyan cinema-list URL")

    @staticmethod
    def _validate_parameters(parameters: list[tuple[str, str]]) -> None:
        keys = [key for key, _ in parameters]
        if len(keys) != len(set(keys)) or set(keys) - ALLOWED_FILTERS:
            raise TargetValidationError("URL has unsupported or duplicate parameters")
        values = dict(parameters)
        if set(values) < {"movieId", "showDate"}:
            raise TargetValidationError("URL requires movieId and showDate")
        if not values["movieId"].isdigit():
            raise TargetValidationError("movieId must be numeric")
        if any(not value for _, value in parameters):
            raise TargetValidationError("URL parameters cannot be empty")

    @staticmethod
    def _parse_show_date(value: str) -> date:
        try:
            show_date = date.fromisoformat(value)
        except ValueError as exc:
            raise TargetValidationError("showDate must be ISO formatted") from exc
        if show_date.isoformat() != value or show_date < timezone.localdate():
            raise TargetValidationError("showDate cannot be in the past")
        return show_date

    def _parse_cinema_cell(self, cell, target: ParsedTarget) -> CinemaAvailability:
        name_link = cell.select_one("a.cinema-name[href]")
        if name_link is None:
            raise PageStructureError("cinema cell has no cinema name")
        name = name_link.get_text(" ", strip=True)
        cinema_id = self._cinema_id_from_path(name_link["href"])
        if not name or cinema_id is None:
            raise PageStructureError("cinema cell has invalid identity")

        booking_url = ""
        buy_link = cell.select_one("a.buy-btn[href]")
        if buy_link is not None and buy_link.get_text(" ", strip=True) == "选座购票":
            booking_url = self._validated_booking_url(buy_link["href"], cinema_id, target)
        return CinemaAvailability(
            cinema_id=cinema_id,
            name=name,
            normalized_name=normalize_cinema_name(name),
            bookable=bool(booking_url),
            booking_url=booking_url,
        )

    @staticmethod
    def _cinema_id_from_path(value: str) -> str | None:
        parsed = urlsplit(value)
        path_parts = parsed.path.split("/")
        if len(path_parts) == 3 and path_parts[1] == "cinema" and path_parts[2].isdigit():
            return path_parts[2]
        return None

    @staticmethod
    def _validated_booking_url(value: str, cinema_id: str, target: ParsedTarget) -> str:
        absolute_url = urljoin(BASE_URL, value)
        parsed = urlsplit(absolute_url)
        try:
            port = parsed.port
        except ValueError:
            return ""
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in MAOYAN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or parsed.path != f"/cinema/{cinema_id}"
            or parsed.fragment
        ):
            return ""
        parameters = parse_qsl(parsed.query, keep_blank_values=True)
        if len(parameters) != len(set(key for key, _ in parameters)):
            return ""
        values = dict(parameters)
        if (
            values.get("movieId") != target.movie_id
            or values.get("showDate") != target.show_date.isoformat()
        ):
            return ""
        return urlunsplit(
            ("https", parsed.hostname.lower(), parsed.path, urlencode(sorted(parameters)), "")
        )
