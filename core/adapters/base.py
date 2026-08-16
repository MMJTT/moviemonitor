from dataclasses import dataclass
from datetime import date


class AdapterError(RuntimeError):
    """Base error for a platform adapter failure."""


class TargetValidationError(AdapterError):
    """Raised when a user-supplied platform URL is unsafe or invalid."""


class RateLimitedError(AdapterError):
    """Raised when the platform rejects or challenges a request."""


class TemporaryPlatformError(AdapterError):
    """Raised for network and server failures that may succeed later."""


class PageStructureError(AdapterError):
    """Raised when the response cannot prove it is the requested list page."""


@dataclass(frozen=True)
class ParsedTarget:
    platform: str
    city_id: int
    city_name: str
    movie_id: str
    show_date: date
    source_url: str
    normalized_url: str
    query_key: str


@dataclass(frozen=True)
class CinemaAvailability:
    cinema_id: str
    name: str
    normalized_name: str
    bookable: bool
    booking_url: str


@dataclass(frozen=True)
class CheckResult:
    target: ParsedTarget
    movie_name: str
    valid_page: bool
    cinemas: tuple[CinemaAvailability, ...]
    content_fingerprint: str
