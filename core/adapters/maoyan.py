import hashlib
import re
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
BOOTSTRAP_URL = f"{BASE_URL}/"
BOOKING_PARAMETERS = frozenset({"movieId", "poi", "showDate"})
CINEMA_IDENTITY_PARAMETERS = frozenset({"movieId", "poi"})
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
CITY_ID_PATTERN = re.compile(r"^\{\s*currentcityid\s*:\s*(\d+)\s*\}$")
MOVIE_ID_PATTERN = re.compile(r"^\{\s*movieid\s*:\s*(\d+)\s*\}$")
SHOW_DATE_PATTERN = re.compile(
    r"^\{\s*TagName\s*:\s*['\"](\d{4}-\d{2}-\d{2})['\"]\s*\}$"
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
            bootstrap_response = self.session.get(
                BOOTSTRAP_URL,
                headers={"User-Agent": DESKTOP_USER_AGENT},
                timeout=15,
                allow_redirects=False,
            )
            if self._is_safe_bootstrap_self_redirect(bootstrap_response):
                bootstrap_response = self.session.get(
                    BOOTSTRAP_URL,
                    headers={"User-Agent": DESKTOP_USER_AGENT},
                    timeout=15,
                    allow_redirects=False,
                )
            self._validate_response_status(bootstrap_response)
            response = self.session.get(
                target.normalized_url,
                headers={"User-Agent": DESKTOP_USER_AGENT},
                cookies={"ci": str(target.city_id)},
                timeout=15,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise TemporaryPlatformError("network request failed") from exc

        self._validate_response_status(response)
        return self.parse_html(response.text, target)

    @staticmethod
    def _is_safe_bootstrap_self_redirect(response) -> bool:
        if not 300 <= response.status_code < 400:
            return False
        location = response.headers.get("Location")
        if not location:
            return False
        try:
            parsed = urlsplit(urljoin(BOOTSTRAP_URL, location))
            port = parsed.port
        except ValueError:
            return False
        return bool(
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() == "www.maoyan.com"
            and parsed.username is None
            and parsed.password is None
            and port is None
            and parsed.path == "/"
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _validate_response_status(response) -> None:
        if 300 <= response.status_code < 400:
            raise PageStructureError("redirected platform response")
        if response.status_code in {403, 429}:
            raise RateLimitedError("platform request rejected")
        if 500 <= response.status_code < 600:
            raise TemporaryPlatformError("platform server error")
        if response.status_code != 200:
            raise TemporaryPlatformError("unexpected platform status")

    def parse_html(self, html: str, target: ParsedTarget) -> CheckResult:
        soup = BeautifulSoup(html, "html.parser")
        if soup.select_one("#verify") is not None:
            raise RateLimitedError("platform verification required")

        body = soup.body
        if body is None:
            raise PageStructureError("response has no page body")
        legacy_markers = (
            body.get("data-city"),
            body.get("data-movie-id"),
            body.get("data-movie-name"),
        )
        if any(marker is not None for marker in legacy_markers):
            movie_name, cinema_root = self._parse_legacy_context(soup, body, target)
        else:
            movie_name, cinema_root = self._parse_live_context(soup, target)

        cinema_cells = cinema_root.select(".cinema-cell")
        no_cinemas = cinema_root.select(".no-cinemas")
        if cinema_cells and no_cinemas:
            raise PageStructureError("response has conflicting list structures")
        if len(no_cinemas) > 1:
            raise PageStructureError("response has ambiguous empty-list structure")
        if no_cinemas:
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
    def _parse_legacy_context(soup, body, target: ParsedTarget):
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
        return movie_name, soup

    @staticmethod
    def _parse_live_context(soup, target: ParsedTarget):
        city_containers = soup.select(".city-container[data-val]")
        selected_cities = soup.select(".city-selected")
        movie_names = soup.select(".movie-brief-container h1.name")
        movie_actions = soup.select(".action[data-val]")
        active_filter_links = soup.select("li.active > a[data-val]")
        active_date_matches = []
        for link in active_filter_links:
            if match := SHOW_DATE_PATTERN.fullmatch(link["data-val"]):
                active_date_matches.append(match)
        cinema_lists = soup.select(".cinemas-list")
        if not all(
            len(nodes) == 1
            for nodes in (
                city_containers,
                selected_cities,
                movie_names,
                movie_actions,
                cinema_lists,
            )
        ) or len(active_date_matches) != 1:
            raise PageStructureError("response has ambiguous live page context")

        city_match = CITY_ID_PATTERN.fullmatch(city_containers[0]["data-val"])
        movie_match = MOVIE_ID_PATTERN.fullmatch(movie_actions[0]["data-val"])
        date_match = active_date_matches[0]
        city_name = selected_cities[0].get_text(" ", strip=True)
        movie_name = movie_names[0].get_text(" ", strip=True)
        if city_match is None or int(city_match.group(1)) != target.city_id:
            raise PageStructureError("response city ID does not match target")
        if city_name != target.city_name:
            raise PageStructureError("response city does not match target")
        if movie_match is None or movie_match.group(1) != target.movie_id:
            raise PageStructureError("response movie does not match target")
        if not movie_name:
            raise PageStructureError("response movie name is missing")
        if date_match is None or date_match.group(1) != target.show_date.isoformat():
            raise PageStructureError("response date does not match target")
        return movie_name, cinema_lists[0]

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
        name_links = cell.select("a.cinema-name[href]")
        if len(name_links) != 1:
            raise PageStructureError("cinema cell has no cinema name")
        name_link = name_links[0]
        name = name_link.get_text(" ", strip=True)
        cinema_id = self._cinema_id_from_url(name_link["href"], target)
        if not name or cinema_id is None:
            raise PageStructureError("cinema cell has invalid identity")

        booking_url = ""
        booking_controls = cell.select(".buy-btn")
        if len(booking_controls) > 1:
            raise PageStructureError("cinema cell has ambiguous booking controls")
        if booking_controls:
            control = booking_controls[0]
            links = [control] if control.name == "a" else control.select("a")
            if links:
                if (
                    len(links) != 1
                    or links[0].get_text(" ", strip=True) != "选座购票"
                    or not links[0].get("href")
                ):
                    raise PageStructureError("cinema cell has invalid booking control")
                buy_link = links[0]
                booking_url = self._validated_booking_url(buy_link["href"], cinema_id, target)
                if not booking_url:
                    raise PageStructureError("cinema cell has invalid booking URL")
            elif control.get_text(" ", strip=True) == "选座购票":
                raise PageStructureError("cinema cell has invalid booking control")
        return CinemaAvailability(
            cinema_id=cinema_id,
            name=name,
            normalized_name=normalize_cinema_name(name),
            bookable=bool(booking_url),
            booking_url=booking_url,
        )

    @staticmethod
    def _cinema_id_from_url(value: str, target: ParsedTarget) -> str | None:
        parsed = urlsplit(urljoin(BASE_URL, value))
        try:
            port = parsed.port
        except ValueError:
            return None
        path_parts = parsed.path.split("/")
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in MAOYAN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
            or len(path_parts) != 3
            or path_parts[1] != "cinema"
            or not path_parts[2].isdigit()
            or parsed.fragment
        ):
            return None

        parameters = parse_qsl(parsed.query, keep_blank_values=True)
        keys = [key for key, _ in parameters]
        if len(keys) != len(set(keys)):
            return None
        if parameters:
            values = dict(parameters)
            if (
                not set(values) <= CINEMA_IDENTITY_PARAMETERS
                or "poi" not in values
                or not values["poi"].isdigit()
                or (
                    "movieId" in values
                    and values["movieId"] != target.movie_id
                )
            ):
                return None
        return path_parts[2]

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
        values = {key: value for key, value in parameters if key in BOOKING_PARAMETERS}
        if values.get("movieId") != target.movie_id:
            return ""
        show_date = values.get("showDate")
        if show_date is not None and show_date != target.show_date.isoformat():
            return ""
        if "poi" in values and not values["poi"].isdigit():
            return ""
        values["showDate"] = target.show_date.isoformat()
        return urlunsplit(
            ("https", parsed.hostname.lower(), parsed.path, urlencode(sorted(values.items())), "")
        )
