# Local Single-Task Ticket Monitor MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a one-command local Django application that monitors one exact Maoyan city/movie/date/cinema target and sends idempotent opening or expiry email.

**Architecture:** Django serves a loopback-only Chinese web UI while a single background thread scans SQLite-owned task and notification due times. A Maoyan adapter isolates URL validation/fetch/parsing, and an SMTP service isolates encrypted credentials and delivery; no Redis, Celery, PostgreSQL, Docker, authentication, or public deployment is used.

**Tech Stack:** Python 3.12, Django 5.2, SQLite, Requests, Beautiful Soup 4, cryptography/Fernet, Django templates, pytest-django, freezegun, responses, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-16-local-single-task-mvp-design.md`

## Global Constraints

- Start the complete app with `python manage.py runlocal`; bind only `127.0.0.1:8000` and disable Django auto-reload so exactly one scheduler thread exists.
- This is an unauthenticated, single-user local application with at most one unfinished task and exactly one cinema per task.
- SQLite is the durable source of truth for task, check, schedule, and notification state; in-memory timers are reconstructible.
- The user edits one fixed polling interval; model and form validation reject values below 60 seconds.
- The server requests only validated HTTPS Maoyan cinema-list URLs, sends city `ci` separately, and rejects redirects.
- Only a valid list page plus exact cinema ID/name match plus an in-card `选座购票` link can create an opening detection.
- Valid empty lists remain waiting; captcha, 403/429, network errors, mismatched query context, and malformed HTML never create detections.
- Each task has at most one `OPENING` and one `EXPIRY` notification, enforced by SQLite uniqueness.
- SMTP authorization codes are Fernet-encrypted using ignored local `.ticketwatch.key`; secrets and full mail bodies never enter logs or rendered pages.
- `monitor.py` remains byte-for-byte equal to baseline hash `b499a42282a87cf5a577cb71cec70ef49536ac9225c0bde48b2af5c1f38178cc`.
- Fixed parser fixtures drive automated tests; test runs never depend on live Maoyan or live SMTP.
- The interrupted full-platform scaffold is uncommitted input, not trusted completed work; Task 1 audits and reconciles it.

---

## Planned File Map

```text
maoyan/
├── manage.py
├── pyproject.toml
├── .env.example
├── .gitignore
├── monitor.py                       # immutable legacy reference
├── ticketwatch/
│   ├── settings.py
│   ├── urls.py
│   ├── health.py
│   └── wsgi.py
├── core/
│   ├── models.py                    # singleton settings + task/check/notification
│   ├── crypto.py                    # local Fernet key boundary
│   ├── forms.py
│   ├── views.py
│   ├── urls.py
│   ├── services/
│   │   ├── smtp.py
│   │   ├── tasks.py
│   │   └── notifications.py
│   ├── adapters/
│   │   ├── base.py
│   │   └── maoyan.py
│   ├── scheduler.py
│   ├── management/commands/runlocal.py
│   └── tests/
├── templates/
│   ├── base.html
│   └── core/
├── static/css/app.css
├── tests/
│   ├── fixtures/maoyan/
│   ├── test_health.py
│   └── test_mvp_flow.py
└── README.md
```

## Task 1: Reconcile and finish the local Django foundation

**Files:**
- Modify: `.gitignore`
- Modify: `.env.example`
- Modify: `pyproject.toml`
- Modify: `ticketwatch/settings.py`
- Modify: `ticketwatch/urls.py`
- Create: `ticketwatch/health.py`
- Modify: `tests/test_health.py`
- Restore: `monitor.py`
- Remove generated artifact: `ticketwatch.egg-info/`

**Interfaces:**
- Consumes: interrupted uncommitted scaffold and baseline commit `e1d7fd2`.
- Produces: installable `ticketwatch` project, SQLite `DATABASES`, loopback-ready settings, `GET /healthz -> {"status": "ok"}`, and a clean immutable `monitor.py`.

- [ ] **Step 1: Audit interrupted files and restore the legacy reference**

```bash
git status --short
git diff -- monitor.py
git restore monitor.py
rm -rf -- ticketwatch.egg-info
shasum -a 256 monitor.py
```

Expected hash: `b499a42282a87cf5a577cb71cec70ef49536ac9225c0bde48b2af5c1f38178cc`.

- [ ] **Step 2: Reduce dependencies to the local MVP**

Replace `pyproject.toml` project dependencies with:

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "ticketwatch"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "Django>=5.2,<5.3",
  "requests>=2.32,<3",
  "beautifulsoup4>=4.13,<5",
  "cryptography>=45,<47",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3,<9",
  "pytest-django>=4.11,<5",
  "pytest-mock>=3.14,<4",
  "freezegun>=1.5,<2",
  "responses>=0.25,<1",
  "ruff>=0.12,<1",
]

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "ticketwatch.settings"
python_files = ["test_*.py"]
addopts = "-ra --strict-markers"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "DJ"]
```

Append these ignore rules:

```gitignore
*.egg-info/
.ticketwatch.key
db.sqlite3
```

Replace `.env.example` with the optional local overrides only:

```dotenv
DJANGO_SECRET_KEY=replace-only-if-you-need-a-stable-local-key
TICKETWATCH_KEY_FILE=.ticketwatch.key
```

- [ ] **Step 3: Verify the health test is red before completing the endpoint**

Ensure `tests/test_health.py` contains:

```python
def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

Run:

```bash
.venv/bin/pytest tests/test_health.py -v
```

Expected: FAIL with 404 or missing `ticketwatch.health` until the explicit endpoint is installed.

- [ ] **Step 4: Complete local settings and health endpoint**

Create `ticketwatch/health.py`:

```python
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"status": "ok"})
```

Use environment-safe local settings:

```python
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-ticketwatch-development-key")
DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
TIME_ZONE = "Asia/Shanghai"
USE_TZ = True
LANGUAGE_CODE = "zh-hans"
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
TICKETWATCH_KEY_FILE = Path(os.environ.get("TICKETWATCH_KEY_FILE", BASE_DIR / ".ticketwatch.key"))
```

Set `TEMPLATES[0]["DIRS"] = [BASE_DIR / "templates"]` and route `healthz` plus `core.urls` when Task 2 creates the app. Until then, root URLs contain only health:

```python
from django.urls import path

from ticketwatch.health import healthz

urlpatterns = [path("healthz", healthz, name="healthz")]
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/test_health.py -v
.venv/bin/python manage.py check
.venv/bin/ruff check ticketwatch tests
shasum -a 256 monitor.py
git add .gitignore .env.example pyproject.toml manage.py ticketwatch tests/test_health.py
git commit -m "build: scaffold local ticket monitor"
```

Expected: health test passes, Django check is clean, Ruff is clean, and legacy hash matches.

## Task 2: Add SQLite domain models and local credential encryption

**Files:**
- Create: `core/__init__.py`
- Create: `core/apps.py`
- Create: `core/models.py`
- Create: `core/crypto.py`
- Create: `core/migrations/0001_initial.py`
- Create: `core/tests/test_models.py`
- Create: `core/tests/test_crypto.py`
- Create: `core/tests/conftest.py`
- Modify: `ticketwatch/settings.py`

**Interfaces:**
- Consumes: Django SQLite configuration and `settings.TICKETWATCH_KEY_FILE`.
- Produces: singleton `AppSetting.get_solo()`, singleton `SMTPConfig.get_solo()`, `MonitorTask`, `CheckRun`, `Notification`, `encrypt_secret(value: str) -> str`, and `decrypt_secret(token: str) -> str`.

- [ ] **Step 1: Write failing model and encryption tests**

Create tests proving the interval floor, singleton behavior, one-active-task constraint, notification uniqueness, encrypted round trip, key permissions, and missing-key-with-existing-ciphertext failure:

```bash
.venv/bin/python manage.py startapp core
mkdir -p core/tests
touch core/tests/__init__.py
```

Create `core/tests/conftest.py` with a real SQLite-backed factory fixture reused by later tasks:

```python
from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import MonitorTask, SMTPConfig


@pytest.fixture
def task_factory(db):
    def create(**overrides):
        values = {
            "source_url": "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            "normalized_url": "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
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
def verified_smtp(db):
    config = SMTPConfig.get_solo()
    config.host = "smtp.example.com"
    config.username = config.from_email = "sender@example.com"
    config.recipient_email = "receiver@example.com"
    config.encrypted_password = "test-ciphertext"
    config.is_verified = True
    config.save()
    return config
```

```python
import os

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from core.crypto import CredentialKeyError, decrypt_secret, encrypt_secret
from core.models import AppSetting, MonitorTask, Notification, SMTPConfig


@pytest.mark.django_db
def test_poll_interval_cannot_be_below_sixty_seconds():
    setting = AppSetting(poll_interval_seconds=59)
    with pytest.raises(ValidationError):
        setting.full_clean()


@pytest.mark.django_db(transaction=True)
def test_only_one_unfinished_task_is_allowed(task_factory):
    task_factory(status=MonitorTask.Status.MONITORING)
    with pytest.raises(IntegrityError):
        task_factory(status=MonitorTask.Status.PAUSED)


@pytest.mark.django_db(transaction=True)
def test_notification_type_is_unique_per_task(task_factory):
    task = task_factory(status=MonitorTask.Status.CANCELLED)
    Notification.objects.create(task=task, notification_type=Notification.Type.OPENING)
    with pytest.raises(IntegrityError):
        Notification.objects.create(task=task, notification_type=Notification.Type.OPENING)


def test_secret_round_trip_uses_private_key_file(settings, tmp_path):
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"
    token = encrypt_secret("authorization-code")
    assert token != "authorization-code"
    assert decrypt_secret(token) == "authorization-code"
    assert os.stat(settings.TICKETWATCH_KEY_FILE).st_mode & 0o777 == 0o600


def test_missing_key_does_not_replace_existing_ciphertext(settings, tmp_path):
    settings.TICKETWATCH_KEY_FILE = tmp_path / ".ticketwatch.key"
    token = encrypt_secret("authorization-code")
    settings.TICKETWATCH_KEY_FILE.unlink()
    with pytest.raises(CredentialKeyError, match="missing"):
        decrypt_secret(token)
```

- [ ] **Step 2: Run tests red**

```bash
.venv/bin/pytest core/tests/test_models.py core/tests/test_crypto.py -v
```

Expected: FAIL because domain types and crypto functions do not exist.

- [ ] **Step 3: Implement encryption boundary**

Create `core/crypto.py`:

```python
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialKeyError(RuntimeError):
    pass


def _load_key(create):
    path = Path(settings.TICKETWATCH_KEY_FILE)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise CredentialKeyError("credential key is missing") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


def encrypt_secret(value):
    return Fernet(_load_key(create=True)).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token):
    try:
        return Fernet(_load_key(create=False)).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialKeyError("credential cannot be decrypted with the local key") from exc
```

- [ ] **Step 4: Implement models and constraints**

Create `core/models.py` with exact choices and constraints:

```python
import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class SingletonModel(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.id = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        value, _ = cls.objects.get_or_create(pk=1)
        return value


class AppSetting(SingletonModel):
    poll_interval_seconds = models.PositiveIntegerField(default=60, validators=[MinValueValidator(60)])
    updated_at = models.DateTimeField(auto_now=True)


class SMTPConfig(SingletonModel):
    class Security(models.TextChoices):
        SSL = "ssl", "SSL/TLS"
        STARTTLS = "starttls", "STARTTLS"

    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(default=465)
    security = models.CharField(max_length=16, choices=Security.choices, default=Security.SSL)
    username = models.CharField(max_length=255, blank=True)
    from_email = models.EmailField(blank=True)
    recipient_email = models.EmailField(blank=True)
    encrypted_password = models.TextField(blank=True)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class MonitorTask(models.Model):
    class Status(models.TextChoices):
        MONITORING = "MONITORING", "监控中"
        PAUSED = "PAUSED", "已暂停"
        DETECTED = "DETECTED", "已检测开票"
        COMPLETED = "COMPLETED", "已完成"
        EXPIRED = "EXPIRED", "已过期"
        CANCELLED = "CANCELLED", "已取消"
        ERROR = "ERROR", "异常"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_url = models.URLField(max_length=1000)
    normalized_url = models.URLField(max_length=1000)
    query_key = models.CharField(max_length=128, db_index=True)
    city_id = models.PositiveIntegerField()
    city_name = models.CharField(max_length=80)
    movie_id = models.CharField(max_length=32)
    movie_name = models.CharField(max_length=200)
    show_date = models.DateField()
    cinema_id = models.CharField(max_length=32, blank=True)
    cinema_name = models.CharField(max_length=200)
    normalized_cinema_name = models.CharField(max_length=200)
    booking_url = models.URLField(max_length=1000, blank=True)
    singleton_slot = models.BooleanField(default=True, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.MONITORING)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    detected_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["singleton_slot"],
                condition=Q(status__in=["MONITORING", "PAUSED", "DETECTED", "ERROR"]),
                name="one_unfinished_monitor_task",
            )
        ]


class CheckRun(models.Model):
    class Status(models.TextChoices):
        SUCCEEDED = "SUCCEEDED", "成功"
        TEMPORARY_ERROR = "TEMPORARY_ERROR", "临时错误"
        RATE_LIMITED = "RATE_LIMITED", "限流"
        STRUCTURE_ERROR = "STRUCTURE_ERROR", "结构错误"
        CONFIG_ERROR = "CONFIG_ERROR", "配置错误"

    task = models.ForeignKey(MonitorTask, on_delete=models.CASCADE, related_name="checks")
    status = models.CharField(max_length=24, choices=Status.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    content_fingerprint = models.CharField(max_length=64, blank=True)
    cinema_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True)
    error_summary = models.CharField(max_length=200, blank=True)


class Notification(models.Model):
    class Type(models.TextChoices):
        OPENING = "OPENING", "开票"
        EXPIRY = "EXPIRY", "到期"

    class Status(models.TextChoices):
        PENDING = "PENDING", "待发送"
        SENDING = "SENDING", "发送中"
        SENT = "SENT", "成功"
        FAILED = "FAILED", "失败"
        PERMANENT_FAILED = "PERMANENT_FAILED", "永久失败"

    task = models.ForeignKey(MonitorTask, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=16, choices=Type.choices)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    message_id = models.CharField(max_length=255, blank=True)
    smtp_response = models.CharField(max_length=200, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "notification_type"], name="unique_task_notification_type")
        ]
```

Add `"core"` to `INSTALLED_APPS` before generating migrations.

- [ ] **Step 5: Migrate, verify, and commit**

```bash
.venv/bin/python manage.py makemigrations core
.venv/bin/python manage.py migrate
.venv/bin/pytest core/tests/test_models.py core/tests/test_crypto.py -v
.venv/bin/python manage.py check
.venv/bin/ruff check core
git add core ticketwatch/settings.py
git commit -m "feat: add local monitoring domain"
```

## Task 3: Implement strict Maoyan URL validation and HTML parsing

**Files:**
- Create: `core/adapters/__init__.py`
- Create: `core/adapters/base.py`
- Create: `core/adapters/maoyan.py`
- Create: `core/tests/test_maoyan_url.py`
- Create: `core/tests/test_maoyan_parser.py`
- Create: `tests/fixtures/maoyan/open.html`
- Create: `tests/fixtures/maoyan/closed.html`
- Create: `tests/fixtures/maoyan/captcha.html`
- Create: `tests/fixtures/maoyan/malformed.html`

**Interfaces:**
- Consumes: city ID/name supplied by a server-owned choice and validated user URL.
- Produces: `ParsedTarget`, `CinemaAvailability`, `CheckResult`, adapter exceptions, `normalize_cinema_name(value: str) -> str`, `MaoyanAdapter.validate_target(url, city_id, city_name) -> ParsedTarget`, and `MaoyanAdapter.fetch(target) -> CheckResult`.

- [ ] **Step 1: Create minimal sanitized fixtures**

```html
<!-- tests/fixtures/maoyan/open.html -->
<html><body data-city="上海" data-movie-id="1545360" data-movie-name="奥德赛">
  <a class="date-item active" data-date="2026-08-20">8月20日</a>
  <div class="cinema-cell">
    <a class="cinema-name" href="/cinema/37534">MOViE MOViE 影城（前滩太古里店）</a>
    <a class="buy-btn" href="/cinema/37534?movieId=1545360&amp;showDate=2026-08-20">选座购票</a>
  </div>
</body></html>
```

```html
<!-- tests/fixtures/maoyan/closed.html -->
<html><body data-city="上海" data-movie-id="1545360" data-movie-name="奥德赛">
  <a class="date-item active" data-date="2026-08-20">8月20日</a>
  <div class="no-cinemas">暂无影院排片</div>
</body></html>
```

```html
<!-- tests/fixtures/maoyan/captcha.html -->
<html><body><div id="verify">访问验证</div></body></html>
```

```html
<!-- tests/fixtures/maoyan/malformed.html -->
<html><body><main>response without expected cinema-list structure</main></body></html>
```

- [ ] **Step 2: Write URL and parser tests first**

```python
@freeze_time("2026-08-16")
def test_valid_target_preserves_source_and_normalizes_query():
    target = MaoyanAdapter().validate_target(
        "https://www.maoyan.com/cinemas?showDate=2026-08-20&brandId=357343&movieId=1545360",
        city_id=10,
        city_name="上海",
    )
    assert target.source_url.endswith("movieId=1545360")
    assert target.movie_id == "1545360"
    assert target.show_date.isoformat() == "2026-08-20"
    assert "brandId=357343" in target.normalized_url


@pytest.mark.parametrize("url", [
    "http://www.maoyan.com/cinemas?movieId=1&showDate=2026-08-20",
    "https://127.0.0.1/cinemas?movieId=1&showDate=2026-08-20",
    "https://example.com/cinemas?movieId=1&showDate=2026-08-20",
    "https://www.maoyan.com/films?movieId=1&showDate=2026-08-20",
])
@freeze_time("2026-08-16")
def test_unsafe_target_is_rejected(url):
    with pytest.raises(TargetValidationError):
        MaoyanAdapter().validate_target(url, 10, "上海")
```

```python
def test_open_page_returns_exact_bookable_cinema():
    result = adapter.parse_html(read_fixture("open.html"), target)
    assert result.valid_page is True
    assert result.cinemas == (
        CinemaAvailability(
            cinema_id="37534",
            name="MOViE MOViE 影城（前滩太古里店）",
            normalized_name="movie movie 影城(前滩太古里店)",
            bookable=True,
            booking_url="https://www.maoyan.com/cinema/37534?movieId=1545360&showDate=2026-08-20",
        ),
    )


def test_closed_page_is_valid_and_empty():
    assert adapter.parse_html(read_fixture("closed.html"), target).cinemas == ()


def test_captcha_and_malformed_are_not_closed():
    with pytest.raises(RateLimitedError):
        adapter.parse_html(read_fixture("captcha.html"), target)
    with pytest.raises(PageStructureError):
        adapter.parse_html(read_fixture("malformed.html"), target)
```

- [ ] **Step 3: Run tests red**

```bash
.venv/bin/pytest core/tests/test_maoyan_url.py core/tests/test_maoyan_parser.py -v
```

Expected: FAIL because adapter modules do not exist.

- [ ] **Step 4: Implement contracts, validation, normalization, parser, and safe fetch**

Define frozen dataclasses in `base.py`:

```python
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
```

`validate_target` allows only HTTPS exact Maoyan hosts, default port, `/cinemas`, one numeric `movieId`, one ISO non-past `showDate`, and known filters `movieId`, `showDate`, `brandId`, `districtId`, `hallType`, `serviceId`. It sorts retained parameters and hashes `maoyan:{city_id}:{normalized_url}` for `query_key`.

```python
def normalize_cinema_name(value):
    value = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"（": "(", "）": ")", "【": "[", "】": "]"})
    ).casefold()
    return " ".join(value.split())
```

`parse_html` verifies body city/movie metadata and active date before recognizing `.cinema-cell` or `.no-cinemas`. `fetch` uses a `requests.Session`, 15-second timeout, desktop User-Agent, `ci` Cookie, and `allow_redirects=False`; redirects are errors, 403/429 map to `RateLimitedError`, network/5xx to `TemporaryPlatformError`, and missing structures to `PageStructureError`.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest core/tests/test_maoyan_url.py core/tests/test_maoyan_parser.py -v
.venv/bin/ruff check core/adapters core/tests/test_maoyan_url.py core/tests/test_maoyan_parser.py
git add core/adapters core/tests tests/fixtures/maoyan
git commit -m "feat: validate and parse maoyan pages"
```

## Task 4: Add local SMTP configuration, encryption, and test mail

**Files:**
- Create: `core/forms.py`
- Create: `core/services/__init__.py`
- Create: `core/services/smtp.py`
- Create: `core/views.py`
- Create: `core/urls.py`
- Create: `core/tests/test_smtp.py`
- Create: `templates/core/smtp_form.html`
- Modify: `ticketwatch/urls.py`

**Interfaces:**
- Consumes: `SMTPConfig`, `encrypt_secret`, `decrypt_secret`, and Django forms/views.
- Produces: `SMTPConfigForm`, `sanitize_smtp_error(exc) -> str`, `send_message(config, message) -> str`, `test_smtp_config(config) -> None`, and routes `core:smtp-edit`/`core:smtp-test`.

- [ ] **Step 1: Write failing SMTP service and view tests**

```python
@pytest.mark.django_db
@patch("core.services.smtp.smtplib.SMTP_SSL")
def test_successful_test_mail_marks_configuration_verified(smtp_ssl, client):
    response = client.post(
        reverse("core:smtp-edit"),
        {
            "host": "smtp.163.com",
            "port": 465,
            "security": "ssl",
            "username": "sender@example.com",
            "from_email": "sender@example.com",
            "recipient_email": "receiver@example.com",
            "authorization_code": "authorization-code",
        },
        follow=True,
    )
    assert response.status_code == 200
    config = SMTPConfig.get_solo()
    assert config.is_verified is True
    assert config.encrypted_password != "authorization-code"
    smtp_ssl.return_value.login.assert_called_once_with("sender@example.com", "authorization-code")


@pytest.mark.django_db
def test_authorization_code_is_never_rendered(client):
    config = SMTPConfig.get_solo()
    config.encrypted_password = encrypt_secret("authorization-code")
    config.save()
    body = client.get(reverse("core:smtp-edit")).content.decode()
    assert "authorization-code" not in body
    assert config.encrypted_password not in body
```

- [ ] **Step 2: Run SMTP tests red**

```bash
.venv/bin/pytest core/tests/test_smtp.py -v
```

Expected: FAIL because service, form, routes, and template are absent.

- [ ] **Step 3: Implement SMTP boundary and form semantics**

`sanitize_smtp_error` returns only exception class and numeric `smtp_code`. `_connect` uses `SMTP_SSL` for SSL and `SMTP` followed by `starttls(ssl.create_default_context())` for STARTTLS, always with 15-second timeout. `send_message` logs no body/credentials, logs in, sends, and closes in `finally`.

```python
class SMTPConfigForm(forms.ModelForm):
    authorization_code = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = SMTPConfig
        fields = ("host", "port", "security", "username", "from_email", "recipient_email")

    def clean_authorization_code(self):
        value = self.cleaned_data["authorization_code"]
        if not value and not self.instance.encrypted_password:
            raise forms.ValidationError("请输入 SMTP 授权码")
        return value

    def save(self, commit=True):
        config = super().save(commit=False)
        secret = self.cleaned_data["authorization_code"]
        if secret:
            config.encrypted_password = encrypt_secret(secret)
            config.is_verified = False
            config.verified_at = None
        if commit:
            config.save()
        return config
```

The POST edit view saves, immediately calls `test_smtp_config`, marks verified only after SMTP acceptance, renders only sanitized errors, and redirects back on success. A separate POST-only test route retests the stored configuration.

- [ ] **Step 4: Add template and routes**

Use standard Django form rendering, an empty password field, CSRF, verification badge, last verified time, sanitized error, Save & Test button, and POST retest form. Add `path("", include("core.urls"))` to root URLs.

```html
<form method="post">
  {% csrf_token %}
  {{ form.as_p }}
  <button class="btn btn-primary" type="submit">保存并发送测试邮件</button>
</form>
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest core/tests/test_smtp.py core/tests/test_crypto.py -v
.venv/bin/ruff check core
git add core templates/core/smtp_form.html ticketwatch/urls.py
git commit -m "feat: configure local smtp delivery"
```

## Task 5: Add signed Maoyan preview and single-task creation

**Files:**
- Modify: `core/forms.py`
- Modify: `core/views.py`
- Modify: `core/urls.py`
- Modify: `core/tests/conftest.py`
- Create: `core/services/tasks.py`
- Create: `core/scheduler.py`
- Create: `core/tests/test_task_creation.py`
- Create: `templates/core/task_preview_form.html`
- Create: `templates/core/task_confirm.html`

**Interfaces:**
- Consumes: verified `SMTPConfig`, `MaoyanAdapter`, domain models, and exact cinema normalization.
- Produces: `TaskPreviewPayload`, `sign_preview(payload) -> str`, `unsign_preview(value) -> dict`, `create_task(signed_preview, cinema_id, manual_name) -> MonitorTask`, and preview/confirm routes.

- [ ] **Step 1: Write failing preview and confirmation tests**

Extend `core/tests/conftest.py`:

```python
@pytest.fixture
def active_task(task_factory):
    return task_factory(status=MonitorTask.Status.MONITORING)


@pytest.fixture
def signed_preview():
    return sign_preview(
        TaskPreviewPayload(
            city_id=10,
            city_name="上海",
            source_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            normalized_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            query_key="maoyan:10:fixture",
            movie_id="1545360",
            movie_name="奥德赛",
            show_date="2026-08-20",
            cinemas=(),
        )
    )
```

```python
@pytest.mark.django_db
def test_preview_requires_verified_smtp(client):
    response = client.post(reverse("core:task-preview"), {"city_id": "10", "source_url": VALID_URL})
    assert response.status_code == 403


@pytest.mark.django_db
def test_confirm_creates_one_manual_cinema_task(client, verified_smtp, mocker):
    payload = TaskPreviewPayload(
        city_id=10,
        city_name="上海",
        source_url=VALID_URL,
        normalized_url=VALID_URL,
        query_key="maoyan:10:fixture",
        movie_id="1545360",
        movie_name="奥德赛",
        show_date="2026-08-20",
        cinemas=(),
    )
    check_now = mocker.patch("core.services.tasks.enqueue_immediate_check")
    response = client.post(
        reverse("core:task-confirm"),
        {"signed_preview": sign_preview(payload), "manual_cinema_name": "MOViE MOViE 影城（前滩太古里店）"},
    )
    assert response.status_code == 302
    task = MonitorTask.objects.get()
    assert task.normalized_cinema_name == "movie movie 影城(前滩太古里店)"
    check_now.assert_called_once_with(task.pk)


@pytest.mark.django_db
def test_second_unfinished_task_is_rejected(client, verified_smtp, active_task, signed_preview):
    response = client.post(reverse("core:task-confirm"), {"signed_preview": signed_preview, "manual_cinema_name": "另一影院"})
    assert response.status_code == 400
```

- [ ] **Step 2: Run creation tests red**

```bash
.venv/bin/pytest core/tests/test_task_creation.py -v
```

Expected: FAIL because creation forms/services/views are absent.

- [ ] **Step 3: Implement server-owned city and signed preview**

For MVP, expose the confirmed Shanghai choice only; adding cities later is data-only:

```python
MAOYAN_CITIES = {10: "上海"}


@dataclass(frozen=True)
class PreviewCinema:
    id: str
    name: str


@dataclass(frozen=True)
class TaskPreviewPayload:
    city_id: int
    city_name: str
    source_url: str
    normalized_url: str
    query_key: str
    movie_id: str
    movie_name: str
    show_date: str
    cinemas: tuple[PreviewCinema, ...]


def sign_preview(payload):
    return signing.dumps(asdict(payload), salt="local-task-preview", compress=True)


def unsign_preview(value):
    return signing.loads(value, salt="local-task-preview", max_age=30 * 60)
```

The preview POST validates city ownership, validates/fetches once, and signs only normalized server-derived values. It renders visible cinemas regardless of whether they are currently bookable, plus one manual-name field.

- [ ] **Step 4: Implement atomic single-task confirmation**

`create_task` requires verified SMTP, exactly one selected visible cinema ID or one non-empty manual name, rejects both/none, rejects any selected ID not in the signed payload, checks no unfinished task exists, and catches the database uniqueness error as a form error. Set `next_check_at=timezone.now()` and invoke `enqueue_immediate_check` only via `transaction.on_commit`.

```python
def enqueue_immediate_check(task_id):
    from core.scheduler import wake_scheduler

    wake_scheduler()
```

This function deliberately wakes the single scheduler rather than creating a second worker/thread.

Task 5 creates the scheduler boundary as a safe pre-scheduler no-op; Task 7 replaces its internals without changing callers:

```python
def wake_scheduler():
    return None
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest core/tests/test_task_creation.py -v
.venv/bin/ruff check core
git add core templates/core/task_preview_form.html templates/core/task_confirm.html
git commit -m "feat: create one signed monitoring task"
```

## Task 6: Implement exact checks and idempotent opening/expiry mail

**Files:**
- Create: `core/services/notifications.py`
- Modify: `core/services/tasks.py`
- Create: `core/tests/test_checks.py`
- Create: `core/tests/test_notifications.py`
- Create: `core/tests/test_expiry.py`
- Modify: `core/tests/conftest.py`

**Interfaces:**
- Consumes: `MaoyanAdapter.fetch`, task/check/notification models, `send_message`, and `AppSetting`.
- Produces: `perform_check(task_id, now=None) -> CheckRun`, `expire_due_task(now=None) -> bool`, `dispatch_due_notifications(now=None) -> int`, and `deliver_notification(notification_id, now=None) -> None`.

- [ ] **Step 1: Write failing exact-match and immediate-opening tests**

Define a literal result helper in `test_checks.py` rather than mocking the service under test:

```python
def result_for(task, cinema_name, bookable):
    target = ParsedTarget(
        platform="maoyan",
        city_id=task.city_id,
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
        valid_page=True,
        cinemas=(
            CinemaAvailability(
                cinema_id="37534",
                name=cinema_name,
                normalized_name=normalize_cinema_name(cinema_name),
                bookable=bookable,
                booking_url="https://www.maoyan.com/cinema/37534",
            ),
        ),
        content_fingerprint="fixture-fingerprint",
    )
```

```python
@pytest.mark.django_db
def test_near_name_does_not_detect(active_task, mocker):
    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", return_value=result_for(active_task, "MOViE MOViE 影城（前滩店）", True))
    perform_check(active_task.pk)
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert not Notification.objects.exists()


@pytest.mark.django_db
def test_exact_opening_creates_one_notification(active_task, mocker):
    mocker.patch("core.services.tasks.MaoyanAdapter.fetch", return_value=result_for(active_task, active_task.cinema_name, True))
    perform_check(active_task.pk)
    perform_check(active_task.pk)
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.DETECTED
    assert Notification.objects.filter(task=active_task, notification_type="OPENING").count() == 1
```

- [ ] **Step 2: Write failing notification and expiry tests**

Extend `core/tests/conftest.py`:

```python
@pytest.fixture
def opening_notification(active_task):
    active_task.status = MonitorTask.Status.DETECTED
    active_task.detected_at = timezone.now()
    active_task.booking_url = "https://www.maoyan.com/cinema/37534"
    active_task.save()
    return Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.PENDING,
    )


@pytest.fixture
def pending_notification(active_task):
    return Notification.objects.create(
        task=active_task,
        notification_type=Notification.Type.OPENING,
        status=Notification.Status.PENDING,
    )
```

```python
@pytest.mark.django_db
def test_successful_opening_mail_completes_task(opening_notification, mocker):
    mocker.patch("core.services.notifications.send_message", return_value="<accepted@ticketwatch.local>")
    deliver_notification(opening_notification.pk)
    opening_notification.refresh_from_db()
    opening_notification.task.refresh_from_db()
    assert opening_notification.status == Notification.Status.SENT
    assert opening_notification.task.status == MonitorTask.Status.COMPLETED


@pytest.mark.django_db
@freeze_time("2026-08-21 00:00:00", tz_offset=8)
def test_expiry_is_timezone_aware_and_idempotent(active_task):
    active_task.show_date = date(2026, 8, 20)
    active_task.save()
    assert expire_due_task() is True
    assert expire_due_task() is False
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.EXPIRED
    assert active_task.notifications.filter(notification_type="EXPIRY").count() == 1
```

- [ ] **Step 3: Run focused tests red**

```bash
.venv/bin/pytest core/tests/test_checks.py core/tests/test_notifications.py core/tests/test_expiry.py -v
```

Expected: FAIL because check/delivery services are absent.

- [ ] **Step 4: Implement check state and failure semantics**

`perform_check` loads an active task, fetches outside a write transaction, then records a `CheckRun`. ID match takes precedence; otherwise normalized full-name equality is required. A bookable match atomically sets `DETECTED`, stores ID/link/time, and `get_or_create`s `OPENING`. A successful valid non-match resets failures and sets `next_check_at = now + AppSetting.poll_interval_seconds`.

Temporary failures use backoff seconds `(120, 300, 900, 1800, 3600)` with `max(configured_interval, backoff)`. Rate/structure errors never detect; the fifth consecutive platform/structure failure sets `ERROR`. Error summaries contain only local categories/codes.

```python
FAILURE_BACKOFF_SECONDS = (120, 300, 900, 1800, 3600)


def matches_target(task, cinema):
    if task.cinema_id:
        return cinema.cinema_id == task.cinema_id
    return cinema.normalized_name == task.normalized_cinema_name


def next_failure_time(now, configured_seconds, failure_count):
    backoff = FAILURE_BACKOFF_SECONDS[min(failure_count - 1, len(FAILURE_BACKOFF_SECONDS) - 1)]
    return now + timedelta(seconds=max(configured_seconds, backoff))
```

- [ ] **Step 5: Implement notification claim, content, retry, and expiry**

Use retry minutes `(1, 5, 15, 30, 60)`. Claim only `PENDING` rows in a short transaction by setting `SENDING` and deterministic `<opening-{pk}@ticketwatch.local>` or `<expiry-{pk}@ticketwatch.local>` Message-ID. Build mail with movie/date/city/cinema, Asia/Shanghai detection/expiry time, source URL, and booking URL for opening. On SMTP acceptance set `SENT`; opening sets task `COMPLETED`, expiry remains `EXPIRED`.

Temporary SMTP errors return to `PENDING` with persisted `next_attempt_at`; after five scheduled retries set `FAILED`. Authentication/recipient errors set `PERMANENT_FAILED` and `SMTPConfig.is_verified=False`. A pre-existing `SENDING` row is never automatically reclaimed.

`expire_due_task` locks the sole task whose `show_date < timezone.localdate(now)` and status is `MONITORING`, `PAUSED`, `DETECTED`, or `ERROR`; it sets `EXPIRED` and creates one `EXPIRY` notification.

```python
RETRY_MINUTES = (1, 5, 15, 30, 60)


def dispatch_due_notifications(now=None):
    now = now or timezone.now()
    ids = list(
        Notification.objects.filter(status=Notification.Status.PENDING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("created_at")
        .values_list("pk", flat=True)
    )
    for notification_id in ids:
        deliver_notification(notification_id, now=now)
    return len(ids)
```

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest core/tests/test_checks.py core/tests/test_notifications.py core/tests/test_expiry.py -v
.venv/bin/ruff check core/services core/tests
git add core/services core/tests
git commit -m "feat: check openings and deliver local alerts"
```

## Task 7: Add the single-process scheduler, runlocal command, and lifecycle actions

**Files:**
- Modify: `core/scheduler.py`
- Create: `core/management/__init__.py`
- Create: `core/management/commands/__init__.py`
- Create: `core/management/commands/runlocal.py`
- Modify: `core/views.py`
- Modify: `core/urls.py`
- Create: `core/tests/test_scheduler.py`
- Create: `core/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: due task/notification services and SQLite timestamps.
- Produces: process-global `wake_scheduler()`, `run_due_work(now=None) -> dict[str, int | bool]`, `LocalScheduler`, `python manage.py runlocal`, and POST pause/resume/cancel routes.

- [ ] **Step 1: Write failing scheduler recovery tests**

```python
@pytest.mark.django_db
def test_due_work_uses_sqlite_state(active_task, pending_notification, mocker):
    check = mocker.patch("core.scheduler.perform_check")
    deliver = mocker.patch("core.scheduler.dispatch_due_notifications", return_value=1)
    now = timezone.now()
    outcome = run_due_work(now=now)
    assert outcome == {"expired": False, "notifications": 1, "checked": True}
    check.assert_called_once_with(active_task.pk, now=now)
    deliver.assert_called_once()


def test_scheduler_lock_skips_reentrant_tick(mocker):
    scheduler = LocalScheduler(interval_seconds=0.01)
    scheduler._check_lock.acquire()
    try:
        assert scheduler.tick() is False
    finally:
        scheduler._check_lock.release()
```

- [ ] **Step 2: Write failing lifecycle tests**

```python
@pytest.mark.django_db
def test_pause_resume_cancel_are_post_only(client, active_task):
    assert client.get(reverse("core:task-pause", args=[active_task.pk])).status_code == 405
    assert client.post(reverse("core:task-pause", args=[active_task.pk])).status_code == 302
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.PAUSED
    assert client.post(reverse("core:task-resume", args=[active_task.pk])).status_code == 302
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.MONITORING
    assert active_task.next_check_at is not None
    assert client.post(reverse("core:task-cancel", args=[active_task.pk])).status_code == 302
    active_task.refresh_from_db()
    assert active_task.status == MonitorTask.Status.CANCELLED
```

- [ ] **Step 3: Run scheduler/lifecycle tests red**

```bash
.venv/bin/pytest core/tests/test_scheduler.py core/tests/test_lifecycle.py -v
```

Expected: FAIL because scheduler, command, and actions are absent.

- [ ] **Step 4: Implement SQLite-driven due-work loop**

`run_due_work` calls expiry first, due notifications second, then checks a `MONITORING` task only when `next_check_at is null or <= now`. `LocalScheduler` owns a `threading.Event` stop/wake primitive and a non-blocking `threading.Lock`. Its loop calls `tick`, catches/logs only sanitized exception class names, then waits at most one second or until woken.

```python
_scheduler = None


def wake_scheduler():
    if _scheduler is not None:
        _scheduler.wake()
```

- [ ] **Step 5: Implement exactly-one-thread runlocal command**

Subclass Django `runserver.Command`, force `addrport="127.0.0.1:8000"`, `use_reloader=False`, and start one `LocalScheduler` before calling `super().handle`; stop/join it in `finally`. Refuse user-supplied non-loopback addresses with `CommandError`.

```python
class Command(RunserverCommand):
    help = "Run the loopback web UI and one local scheduler"

    def handle(self, *args, **options):
        requested = options.get("addrport")
        if requested and requested not in {"127.0.0.1:8000", "localhost:8000"}:
            raise CommandError("runlocal only listens on 127.0.0.1:8000")
        options["addrport"] = "127.0.0.1:8000"
        options["use_reloader"] = False
        scheduler = LocalScheduler()
        set_process_scheduler(scheduler)
        scheduler.start()
        try:
            return super().handle(*args, **options)
        finally:
            scheduler.stop()
            scheduler.join(timeout=5)
            set_process_scheduler(None)
```

- [ ] **Step 6: Implement lifecycle transitions and verify**

Pause accepts only `MONITORING`/`ERROR`; resume accepts `PAUSED`/`ERROR`, resets failures/errors, sets `MONITORING` and `next_check_at=now`, then wakes scheduler; cancel accepts any unfinished status and sets `CANCELLED`/timestamp. All views use UUID lookup and POST + CSRF.

```bash
.venv/bin/pytest core/tests/test_scheduler.py core/tests/test_lifecycle.py -v
.venv/bin/python manage.py runlocal --help
.venv/bin/ruff check core/scheduler.py core/management core/views.py
git add core
git commit -m "feat: run local web and scheduler together"
```

## Task 8: Complete the Chinese UI, settings, history, and MVP acceptance

**Files:**
- Create: `templates/base.html`
- Create: `templates/core/dashboard.html`
- Create: `templates/core/task_detail.html`
- Create: `templates/core/settings_form.html`
- Modify: `templates/core/smtp_form.html`
- Modify: `templates/core/task_preview_form.html`
- Modify: `templates/core/task_confirm.html`
- Create: `static/css/app.css`
- Modify: `core/forms.py`
- Modify: `core/views.py`
- Modify: `core/urls.py`
- Create: `tests/test_mvp_flow.py`
- Create: `README.md`

**Interfaces:**
- Consumes: all services and routes from Tasks 1–7.
- Produces: dashboard `/`, settings `/settings/`, task detail/history, complete local workflow, and documented start/stop/recovery instructions.

- [ ] **Step 1: Write failing user-visible flow tests**

```python
@pytest.mark.django_db
def test_dashboard_guides_first_run_to_smtp(client):
    body = client.get(reverse("core:dashboard")).content.decode()
    assert "先配置并测试 SMTP" in body
    assert reverse("core:smtp-edit") in body


@pytest.mark.django_db
def test_poll_interval_form_rejects_fifty_nine(client):
    response = client.post(reverse("core:settings"), {"poll_interval_seconds": 59})
    assert response.status_code == 200
    assert "不能低于 60 秒" in response.content.decode()


@pytest.mark.django_db
def test_task_detail_shows_recent_checks_and_notifications(client, active_task):
    CheckRun.objects.create(
        task=active_task,
        status=CheckRun.Status.SUCCEEDED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
    )
    Notification.objects.create(task=active_task, notification_type=Notification.Type.OPENING)
    body = client.get(reverse("core:task-detail", args=[active_task.pk])).content.decode()
    assert "最近检查" in body
    assert "通知记录" in body
```

- [ ] **Step 2: Run UI tests red**

```bash
.venv/bin/pytest tests/test_mvp_flow.py -v
```

Expected: FAIL because dashboard/settings/detail templates and routes are incomplete.

- [ ] **Step 3: Implement dashboard, settings, detail, and local styling**

Use semantic Django templates and `static/css/app.css`, with no CDN or JavaScript dependency. Dashboard states:

- SMTP missing/unverified: show configuration call-to-action;
- SMTP verified and no unfinished task: show new-task call-to-action;
- active task: show movie/date/cinema/status, last/next check, sanitized error, and allowed actions;
- terminal task: show outcome and allow creation of a new task.

`AppSettingForm` uses `MinValueValidator(60)` and Chinese error `轮询间隔不能低于 60 秒`; successful save wakes the scheduler and, for an active task, recomputes `next_check_at` no earlier than now + new interval.

Task detail displays the 20 newest checks and notifications, never full response HTML, SMTP response text, credential ciphertext, or full mail body.

```python
class AppSettingForm(forms.ModelForm):
    class Meta:
        model = AppSetting
        fields = ("poll_interval_seconds",)

    def clean_poll_interval_seconds(self):
        value = self.cleaned_data["poll_interval_seconds"]
        if value < 60:
            raise forms.ValidationError("轮询间隔不能低于 60 秒")
        return value


def dashboard(request):
    task = MonitorTask.objects.order_by("-created_at").first()
    return render(
        request,
        "core/dashboard.html",
        {"task": task, "smtp": SMTPConfig.get_solo(), "setting": AppSetting.get_solo()},
    )
```

- [ ] **Step 4: Document and test the local closed loop**

README exact startup:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runlocal
```

Document `http://127.0.0.1:8000`, terminal-stop behavior, `.ticketwatch.key` backup warning, SMTP authorization codes, single active task, 60-second floor, and how to resume after `ERROR`.

Add an integration test using fixed Maoyan HTML and mocked SMTP that saves/verifies SMTP, previews/confirms a task, runs due work, detects opening, sends once, and shows `COMPLETED` on the dashboard. Assert a second due-work pass does not send again.

- [ ] **Step 5: Run the full acceptance gate**

```bash
.venv/bin/pytest -v
.venv/bin/ruff check . --exclude monitor.py
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
shasum -a 256 monitor.py
git diff --check
```

Expected: all tests pass with no warnings, Ruff/check/migration gates pass, and legacy hash is `b499a42282a87cf5a577cb71cec70ef49536ac9225c0bde48b2af5c1f38178cc`.

- [ ] **Step 6: Commit the MVP**

```bash
git add templates static core tests README.md ticketwatch
git commit -m "feat: complete local ticket monitor MVP"
git status --short
```

Expected: clean feature worktree.

## Completion Gate

Do not call the MVP complete until the full suite passes, `runlocal --help` succeeds, migrations are current, `monitor.py` matches its baseline hash, and the integration test proves: tested encrypted SMTP → signed single-cinema task → immediate/due check → exact bookable match → one accepted opening mail → completed task → no duplicate on a second scheduler pass.
