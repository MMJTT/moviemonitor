# Ticket Monitoring Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy an invite-only multi-user Django service that monitors exact Maoyan city/movie/date/cinema combinations and sends one reliable SMTP notification per cinema when booking opens.

**Architecture:** A Django modular monolith owns users, tasks, state, permissions, and server-rendered pages. Celery workers use Redis for coordination while PostgreSQL remains the source of truth; a platform-adapter boundary isolates Maoyan HTML parsing so future platforms can be added without changing task or notification logic. Nginx terminates HTTPS on the Alibaba Cloud public IP, and Docker Compose runs all application services except host-managed Certbot and encrypted backups.

**Tech Stack:** Python 3.12, Django 5.2 LTS, Django templates + Bootstrap 5, PostgreSQL 17, Redis 7, Celery 5, Beautiful Soup 4, Requests, cryptography/Fernet, pytest-django, factory-boy, Ruff, Gunicorn, Nginx, Docker Compose, Certbot 5.4+, age.

**Spec:** `docs/superpowers/specs/2026-08-16-ticket-monitoring-platform-design.md`

## Global Constraints

- Phase 1 supports Maoyan movie booking pages only; Damai remains a future adapter and automatic purchasing is out of scope.
- A target is the exact tuple of platform, city, movie, requested date, and one to twenty cinemas.
- Users enter the city separately, paste a filtered Maoyan URL, and may choose visible cinemas and/or enter cinema names manually.
- Manual cinema matching requires exact equality after Unicode NFKC, case folding, bracket normalization, whitespace collapsing, and trimming; platform cinema ID wins when available.
- The default polling bands are 60 seconds within 48 hours, 300 seconds for 2–7 days, and 900 seconds beyond 7 days. An administrator may edit all three globally; every configured/effective interval is validated to remain at least 60 seconds. Identical query keys share one fetch, bounded jitter, a Redis lock, and failure backoff.
- A valid empty result means not open; captcha, rate limiting, network failure, redirects, and page-structure changes are errors and must never produce an opening detection.
- Each cinema receives at most one opening notification; a multi-cinema task completes only after every cinema's opening email is accepted by SMTP.
- A task created after booking has opened performs an immediate first check and sends the same idempotent opening notification.
- At the first Asia/Shanghai midnight after the requested date, unfinished tasks expire and send one expiry notification.
- Opening mail contains only movie, date, city, cinema, detection time, the validated original monitoring URL, and booking link; price and showtime parsing are excluded.
- Public registration is disabled. Administrators invite users, and every ordinary queryset is scoped to `request.user`.
- Each user owns one SMTP configuration. Its authorization code is Fernet-encrypted at rest, never returned to the browser, and never written to logs.
- PostgreSQL is the durable source of truth. Redis is limited to Celery transport, coordination, locks, and reconstructible cache state.
- Deployment target is Alibaba Cloud Linux at public IP `47.116.69.108`; private address `172.24.46.143` is not a public application endpoint.
- Only TCP 80 and 443 are published. PostgreSQL, Redis, Gunicorn, and Celery remain on the internal Compose network.
- HTTPS uses a Let’s Encrypt public-IP short-lived certificate with Certbot 5.4 or newer, automated renewal, pre-reload validation, and expiry alerting.
- PostgreSQL backups are encrypted before touching persistent storage, retain seven daily plus four weekly copies, and must pass a restore rehearsal.

---

## Scope and execution order

This plan is intentionally sequential:

1. Establish a Git baseline and isolated worktree.
2. Scaffold Django and define the custom user before the first migration.
3. Add invitations and per-user encrypted SMTP.
4. Add monitoring models and state transitions.
5. Implement and test the Maoyan adapter with fixed HTML fixtures.
6. Add task creation, initial checks, grouping, polling, notifications, and expiry.
7. Add user/admin pages and security hardening.
8. Containerize, deploy HTTPS, add backups, and run acceptance tests.

The existing `monitor.py` remains unchanged as a legacy reference throughout implementation.

## Spec traceability

| Confirmed specification area | Implemented and verified in |
|---|---|
| Invite-only accounts, roles, isolation, login records | Tasks 3, 8, 12, 13 |
| Per-user encrypted/tested SMTP | Tasks 4, 10, 12 |
| URL validation, SSRF defense, city Cookie, adapter boundary | Tasks 6–8 |
| Exact cinema matching and opening semantics | Tasks 5, 7, 10 |
| Adaptive 60-second-floor polling, grouping, locks, recovery | Task 9 |
| Idempotent opening/expiry mail and lifecycle state | Tasks 5, 10, 11 |
| User/admin pages, pause controls, audit, retention, circuit breaker | Tasks 12–13 |
| Internal-only PostgreSQL/Redis and Compose recovery | Task 14 |
| Public-IP HTTPS issuance, renewal, and alert threshold | Task 15 |
| Encrypted backups, restore rehearsal, and all 14 acceptance criteria | Task 16 |

## Planned file map

```text
maoyan/
├── manage.py
├── pyproject.toml
├── .env.example
├── .gitignore
├── .dockerignore
├── docker-compose.yml
├── Dockerfile
├── monitor.py                         # unchanged legacy reference
├── ticketwatch/
│   ├── __init__.py
│   ├── celery.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── accounts/
│   ├── admin.py
│   ├── forms.py
│   ├── models.py                      # User, Invitation
│   ├── rate_limits.py                 # login/invitation throttling
│   ├── services.py                    # invitation lifecycle
│   ├── urls.py
│   ├── views.py
│   └── tests/
├── mailsettings/
│   ├── crypto.py                      # credential encryption boundary
│   ├── forms.py
│   ├── models.py                      # SMTPConfig
│   ├── services.py                    # connection and test mail
│   ├── urls.py
│   ├── views.py
│   └── tests/
├── monitoring/
│   ├── adapters/
│   │   ├── base.py                    # normalized adapter contracts
│   │   └── maoyan.py                  # URL validation, fetch, parse
│   ├── admin.py
│   ├── forms.py
│   ├── models.py                      # tasks, cinemas, checks, notifications, policy
│   ├── polling.py                     # interval and query grouping
│   ├── services.py                    # creation and result application
│   ├── tasks.py                       # Celery entry points
│   ├── urls.py
│   ├── views.py
│   └── tests/
├── audit/
│   ├── models.py                      # AuditEvent
│   ├── services.py
│   └── tests/
├── templates/
│   ├── base.html
│   ├── registration/login.html
│   ├── accounts/
│   ├── mailsettings/
│   └── monitoring/
├── static/css/app.css
├── tests/fixtures/maoyan/
│   ├── open.html
│   ├── closed.html
│   ├── captcha.html
│   └── malformed.html
├── deploy/nginx/
│   ├── bootstrap.conf
│   ├── https.conf
│   └── active.conf
├── scripts/
│   ├── backup_postgres.sh
│   ├── install_ip_certificate.sh
│   ├── renew_ip_certificate.sh
│   ├── restore_postgres.sh
│   └── verify_deployment.sh
└── docs/runbooks/
    ├── deploy.md
    ├── backup-restore.md
    └── incident-response.md
```

## Phase 1: Repository and application foundation

### Task 1: Create the Git baseline and isolated worktree

**Files:**
- Create: `.gitignore`
- Preserve: `monitor.py`
- Preserve: `docs/superpowers/specs/2026-08-16-ticket-monitoring-platform-design.md`
- Preserve: `docs/superpowers/plans/2026-08-16-ticket-monitoring-platform-implementation.md`

**Interfaces:**
- Consumes: the existing non-Git workspace and immutable legacy reference `monitor.py`.
- Produces: Git repository `maoyan`, baseline branch `main`, and isolated execution worktree `../maoyan-worktrees/ticket-monitor` on branch `feat/ticket-monitor`.

- [ ] **Step 1: Verify the pre-implementation workspace**

Run:

```bash
find . -maxdepth 5 -type f | sort
shasum -a 256 monitor.py
```

Expected: only the legacy script and planning documents exist; record the `monitor.py` hash in the execution notes.

- [ ] **Step 2: Initialize Git and add ignore rules**

Create `.gitignore` with exactly:

```gitignore
.DS_Store
.env
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
staticfiles/
media/
backups/
logs/
*.sqlite3
```

Run:

```bash
git init -b main
git add .gitignore monitor.py docs
git commit -m "chore: establish ticket monitor baseline"
```

Expected: one clean baseline commit on `main`.

- [ ] **Step 3: Create the feature worktree**

Run:

```bash
mkdir -p ../maoyan-worktrees
git worktree add ../maoyan-worktrees/ticket-monitor -b feature/ticket-monitor
git -C ../maoyan-worktrees/ticket-monitor status --short --branch
```

Expected: branch `feature/ticket-monitor` is clean. Execute all remaining tasks from `../maoyan-worktrees/ticket-monitor`.

### Task 2: Scaffold Django, tests, and health endpoint

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `manage.py`
- Create: `ticketwatch/__init__.py`
- Create: `ticketwatch/settings.py`
- Create: `ticketwatch/urls.py`
- Create: `ticketwatch/wsgi.py`
- Create: `ticketwatch/health.py`
- Create: `tests/test_health.py`

**Interfaces:**
- Consumes: the Git/worktree baseline from Task 1 and process environment variables documented in `.env.example`.
- Produces: Django settings module `ticketwatch.settings`, WSGI callable `ticketwatch.wsgi.application`, `database_config() -> dict[str, object]`, and unauthenticated `GET /healthz -> {"status": "ok"}`.

- [ ] **Step 1: Declare the Python project and development tools**

Create `pyproject.toml`:

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
  "celery[redis]>=5.5,<6",
  "psycopg[binary]>=3.2,<4",
  "requests>=2.32,<3",
  "beautifulsoup4>=4.13,<5",
  "cryptography>=45,<47",
  "argon2-cffi>=23.1,<26",
  "gunicorn>=23,<24",
  "whitenoise>=6.9,<7",
  "django-bootstrap5>=25,<26",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3,<9",
  "pytest-django>=4.11,<5",
  "pytest-cov>=6,<7",
  "factory-boy>=3.3,<4",
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

Create `.env.example`:

```dotenv
DJANGO_SECRET_KEY=replace-with-a-random-production-value
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=47.116.69.108,localhost,127.0.0.1
MESSAGE_ID_DOMAIN=47.116.69.108
POSTGRES_USER=ticketwatch
POSTGRES_PASSWORD=replace-database-password
POSTGRES_DB=ticketwatch
DATABASE_URL=postgresql://ticketwatch:replace-database-password@postgres:5432/ticketwatch
REDIS_URL=redis://redis:6379/0
SMTP_ENCRYPTION_KEY=replace-with-a-fernet-key
DJANGO_SUPERUSER_EMAIL=admin@example.invalid
DJANGO_SUPERUSER_PASSWORD=replace-admin-password
CERTBOT_EMAIL=admin@example.invalid
BACKUP_AGE_RECIPIENT=age1replacewithrealrecipient
```

Run:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/django-admin startproject ticketwatch .
```

Expected: Django scaffold files exist.

- [ ] **Step 2: Write the failing health endpoint test**

Create `tests/test_health.py`:

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

Expected: FAIL with 404 for `/healthz`.

- [ ] **Step 3: Implement the health endpoint and environment settings**

Create `ticketwatch/health.py`:

```python
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"status": "ok"})
```

Replace `ticketwatch/urls.py` with:

```python
from django.contrib import admin
from django.urls import path

from ticketwatch.health import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
]
```

Update generated `ticketwatch/settings.py` so it reads secrets from environment, uses PostgreSQL when `DATABASE_URL` is present, keeps SQLite only for tests without that variable, installs WhiteNoise and `django_bootstrap5`, sets `TIME_ZONE = "Asia/Shanghai"`, `USE_TZ = True`, Argon2 first in `PASSWORD_HASHERS`, secure proxy headers, and `LOGIN_URL = "login"`.

Add this complete URL parser near the top of settings:

```python
import os
from urllib.parse import urlparse


def database_config():
    raw = os.getenv("DATABASE_URL")
    if not raw:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    parsed = urlparse(raw)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username,
        "PASSWORD": parsed.password,
        "HOST": parsed.hostname,
        "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 60,
    }
```

Set `DATABASES = {"default": database_config()}`, `STATIC_ROOT = BASE_DIR / "staticfiles"`, and the deterministic mail identifier domain:

```python
MESSAGE_ID_DOMAIN = os.environ.get("MESSAGE_ID_DOMAIN", "localhost")
```

- [ ] **Step 4: Verify scaffold quality**

Run:

```bash
.venv/bin/pytest tests/test_health.py -v
.venv/bin/ruff check .
.venv/bin/python manage.py check
```

Expected: one passing test, Ruff clean, Django system check reports no issues.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .env.example manage.py ticketwatch tests
git commit -m "feat: scaffold django service"
```

### Task 3: Add the custom user, invitations, and audit events

**Files:**
- Create: `accounts/models.py`
- Create: `accounts/services.py`
- Create: `accounts/forms.py`
- Create: `accounts/views.py`
- Create: `accounts/urls.py`
- Create: `accounts/admin.py`
- Create: `accounts/tests/test_invitations.py`
- Create: `audit/models.py`
- Create: `audit/services.py`
- Modify: `ticketwatch/settings.py`
- Modify: `ticketwatch/urls.py`

**Interfaces:**
- Consumes: Django project and database configuration from Task 2.
- Produces: `accounts.User`, `accounts.Invitation`, `audit.AuditEvent`, `create_invitation(actor: User, email: str) -> tuple[Invitation, str]`, `accept_invitation(raw_token: str, password: str) -> User`, and `record_event(actor: User | None, action: str, target: Model | None = None, metadata: dict | None = None) -> AuditEvent`.

- [ ] **Step 1: Generate apps and write invitation tests first**

Run:

```bash
.venv/bin/python manage.py startapp accounts
.venv/bin/python manage.py startapp audit
mkdir -p accounts/tests audit/tests
touch accounts/tests/__init__.py audit/tests/__init__.py
```

Create `accounts/tests/test_invitations.py`:

```python
import pytest
from django.utils import timezone

from accounts.models import Invitation, User
from accounts.services import accept_invitation, create_invitation


@pytest.mark.django_db
def test_admin_can_create_and_user_can_accept_invitation():
    admin = User.objects.create_superuser(email="admin@example.com", password="strong-pass")
    invitation, raw_token = create_invitation(admin, "person@example.com")

    user = accept_invitation(raw_token, "new-strong-pass")

    invitation.refresh_from_db()
    assert user.email == "person@example.com"
    assert user.is_active is True
    assert invitation.used_at is not None


@pytest.mark.django_db
def test_expired_invitation_is_rejected():
    admin = User.objects.create_superuser(email="admin@example.com", password="strong-pass")
    invitation, raw_token = create_invitation(admin, "person@example.com")
    Invitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now())

    with pytest.raises(ValueError, match="expired"):
        accept_invitation(raw_token, "new-strong-pass")
```

Run:

```bash
.venv/bin/pytest accounts/tests/test_invitations.py -v
```

Expected: FAIL because the models and services do not exist.

- [ ] **Step 2: Implement the custom user and invitation token lifecycle**

Implement `accounts/models.py` with:

```python
import hashlib
import uuid

from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("email is required")
        user = self.model(email=self.normalize_email(email), username=None, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.update(is_staff=True, is_superuser=True, is_active=True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    updated_at = models.DateTimeField(auto_now=True)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="invitations")
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def hash_token(raw_token):
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
```

Implement `accounts/services.py` with `secrets.token_urlsafe(32)`, a 48-hour expiry, `transaction.atomic()`, `select_for_update()`, one-time use, case-normalized email, and the two public functions exercised by the tests.

- [ ] **Step 3: Add audit events and invitation views**

Implement `audit/models.py`:

```python
from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
```

Implement `audit/services.py`:

```python
from audit.models import AuditEvent


SENSITIVE_KEYS = {"password", "authorization", "smtp_password", "cookie", "token"}


def sanitize_metadata(value):
    if isinstance(value, dict):
        return {
            key: sanitize_metadata(item)
            for key, item in value.items()
            if key.lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_metadata(item) for item in value]
    return value


def record_event(actor, action, target=None, metadata=None):
    clean = sanitize_metadata(metadata or {})
    return AuditEvent.objects.create(
        actor=actor,
        action=action,
        target_type=target.__class__.__name__ if target else "",
        target_id=str(target.pk) if target else "",
        metadata=clean,
    )
```

Add staff-only invitation creation and public token activation views. Activation must set a password, mark the invitation used, log an audit event, and log the new user in. Add Django authentication URLs and templates later in Task 11.

- [ ] **Step 4: Configure and migrate before any default auth migration**

Add `accounts`, `audit`, and `AUTH_USER_MODEL = "accounts.User"` to settings before running migrations.

Run:

```bash
.venv/bin/python manage.py makemigrations accounts audit
.venv/bin/python manage.py migrate
.venv/bin/pytest accounts/tests/test_invitations.py -v
.venv/bin/python manage.py check
```

Expected: both invitation tests pass and migrations complete.

- [ ] **Step 5: Commit**

```bash
git add accounts audit ticketwatch
git commit -m "feat: add invite-only user accounts"
```

### Task 4: Add encrypted per-user SMTP settings

**Files:**
- Create: `mailsettings/crypto.py`
- Create: `mailsettings/models.py`
- Create: `mailsettings/services.py`
- Create: `mailsettings/forms.py`
- Create: `mailsettings/tests/test_smtp.py`
- Modify: `ticketwatch/settings.py`

**Interfaces:**
- Consumes: `accounts.User`, `audit.record_event()`, and `settings.SMTP_ENCRYPTION_KEY`.
- Produces: `SMTPConfig`, `encrypt_secret(value: str) -> str`, `decrypt_secret(value: str) -> str`, `test_smtp_config(config: SMTPConfig, recipient: str) -> None`, and `send_user_message(config: SMTPConfig, message: EmailMessage) -> str` returning the SMTP Message-ID after acceptance.

- [ ] **Step 1: Write encryption and SMTP test failures**

Create `mailsettings/tests/test_smtp.py`:

```python
from unittest.mock import patch

import pytest

from accounts.models import User
from mailsettings.crypto import decrypt_secret, encrypt_secret
from mailsettings.models import SMTPConfig
from mailsettings.services import test_smtp_config


def test_secret_round_trip(settings):
    settings.SMTP_ENCRYPTION_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    ciphertext = encrypt_secret("smtp-authorization-code")
    assert ciphertext != "smtp-authorization-code"
    assert decrypt_secret(ciphertext) == "smtp-authorization-code"


@pytest.mark.django_db
@patch("mailsettings.services.smtplib.SMTP_SSL")
def test_successful_smtp_test_marks_config_verified(smtp_ssl):
    user = User.objects.create_user(email="person@example.com", password="strong-pass")
    config = SMTPConfig.objects.create(
        user=user,
        host="smtp.163.com",
        port=465,
        security=SMTPConfig.Security.SSL,
        username="person@163.com",
        from_email="person@163.com",
    )
    config.set_password("authorization-code")
    config.save()

    test_smtp_config(config, recipient="person@example.com")

    config.refresh_from_db()
    assert config.is_verified is True
    smtp_ssl.return_value.send_message.assert_called_once()
```

Run:

```bash
.venv/bin/python manage.py startapp mailsettings
mkdir -p mailsettings/tests
touch mailsettings/tests/__init__.py
.venv/bin/pytest mailsettings/tests/test_smtp.py -v
```

Expected: FAIL because SMTPConfig and encryption functions do not exist.

- [ ] **Step 2: Implement encryption and SMTPConfig**

Implement `mailsettings/crypto.py`:

```python
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    return Fernet(settings.SMTP_ENCRYPTION_KEY.encode("ascii"))


def encrypt_secret(value):
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("SMTP credential cannot be decrypted") from exc
```

Implement `SMTPConfig` as one-to-one with User, with security choices `ssl` and `starttls`, encrypted password text, verification status/time, and last error. Expose `set_password()` and `get_password()` methods; never include the encrypted field in `__str__`, forms, admin list displays, or serialized responses.

- [ ] **Step 3: Implement connection and test mail**

Implement `mailsettings/services.py` using `smtplib.SMTP_SSL` for SSL and `smtplib.SMTP` plus `starttls(context=ssl.create_default_context())` for STARTTLS. Use `EmailMessage` from the standard library, log no credentials, close connections in `finally`, set `is_verified` only after `send_message` succeeds, and persist a sanitized error otherwise.

```python
import smtplib
import ssl
from email.message import EmailMessage

from django.utils import timezone


def _connect(config):
    if config.security == config.Security.SSL:
        return smtplib.SMTP_SSL(config.host, config.port, timeout=15, context=ssl.create_default_context())
    client = smtplib.SMTP(config.host, config.port, timeout=15)
    client.starttls(context=ssl.create_default_context())
    return client


def sanitize_smtp_error(exc):
    """Return only an exception class and numeric SMTP code, never server text."""
    code = getattr(exc, "smtp_code", "")
    return f"{exc.__class__.__name__}:{code}"[:200]


def send_user_message(config, message):
    client = _connect(config)
    try:
        client.login(config.username, config.get_password())
        client.send_message(message)
        return message["Message-ID"]
    finally:
        try:
            client.quit()
        except smtplib.SMTPException:
            client.close()


def test_smtp_config(config, recipient):
    message = EmailMessage()
    message["Subject"] = "TicketWatch SMTP test"
    message["From"] = config.from_email
    message["To"] = recipient
    message["Message-ID"] = f"<smtp-test-{config.user_id}@ticketwatch>"
    message.set_content("Your TicketWatch SMTP configuration works.")
    try:
        send_user_message(config, message)
    except (OSError, smtplib.SMTPException) as exc:
        config.is_verified = False
        config.last_error = sanitize_smtp_error(exc)
        config.save(update_fields=["is_verified", "last_error", "updated_at"])
        raise
    config.is_verified = True
    config.verified_at = timezone.now()
    config.last_error = ""
    config.save(update_fields=["is_verified", "verified_at", "last_error", "updated_at"])
```

- [ ] **Step 4: Migrate and verify**

Add `mailsettings` and `SMTP_ENCRYPTION_KEY = os.environ["SMTP_ENCRYPTION_KEY"]` to settings.

Run:

```bash
.venv/bin/python manage.py makemigrations mailsettings
.venv/bin/python manage.py migrate
.venv/bin/pytest mailsettings/tests/test_smtp.py -v
.venv/bin/ruff check mailsettings
```

Expected: both tests pass; Ruff is clean.

- [ ] **Step 5: Commit**

```bash
git add mailsettings ticketwatch
git commit -m "feat: add encrypted user smtp settings"
```

## Phase 2: Monitoring domain and Maoyan adapter

### Task 5: Define monitoring models and state transitions

**Files:**
- Create: `monitoring/models.py`
- Create: `monitoring/state.py`
- Create: `monitoring/tests/test_state.py`
- Modify: `ticketwatch/settings.py`

**Interfaces:**
- Consumes: `accounts.User` and Django timezone/database primitives.
- Produces: `MonitorTask`, `TaskCinema`, `CheckRun`, `Notification`, their database constraints, and `refresh_task_status(task: MonitorTask) -> str`.

- [ ] **Step 1: Write state transition tests**

Create `monitoring/tests/test_state.py`:

```python
import pytest

from accounts.models import User
from monitoring.models import MonitorTask, TaskCinema
from monitoring.state import refresh_task_status


@pytest.mark.django_db
def test_multi_cinema_task_moves_partial_then_completed():
    user = User.objects.create_user(email="person@example.com", password="strong-pass")
    task = MonitorTask.objects.create(
        user=user,
        city_name="上海",
        city_id=10,
        source_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
        normalized_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
        movie_id="1545360",
        movie_name="奥德赛",
        show_date="2026-08-20",
        query_key="maoyan:10:fixture-query",
        cinema_set_hash="fixture-cinema-set",
        status=MonitorTask.Status.MONITORING,
    )
    first = TaskCinema.objects.create(task=task, display_name="影院甲", normalized_name="影院甲")
    second = TaskCinema.objects.create(task=task, display_name="影院乙", normalized_name="影院乙")

    first.status = TaskCinema.Status.NOTIFIED
    first.save(update_fields=["status"])
    refresh_task_status(task)
    task.refresh_from_db()
    assert task.status == MonitorTask.Status.PARTIAL

    second.status = TaskCinema.Status.NOTIFIED
    second.save(update_fields=["status"])
    refresh_task_status(task)
    task.refresh_from_db()
    assert task.status == MonitorTask.Status.COMPLETED
```

Run:

```bash
.venv/bin/python manage.py startapp monitoring
mkdir -p monitoring/tests
touch monitoring/tests/__init__.py
.venv/bin/pytest monitoring/tests/test_state.py -v
```

Expected: FAIL because monitoring models do not exist.

- [ ] **Step 2: Implement the domain models**

Create models matching the confirmed spec:

- `MonitorTask.Status`: `MONITORING`, `PARTIAL`, `PAUSED`, `ERROR`, `COMPLETED`, `EXPIRED`, and `CANCELLED`; fields are UUID primary key, user, platform, source/normalized URL and retained normalized filter parameters, city, movie, show date, query key, cinema-set hash, base interval, next/last-success/last-failure timestamps, consecutive fetch/structure failure counters, last sanitized error, explicit started/paused/completed/expired/cancelled lifecycle timestamps, and timestamps.
- `TaskCinema.Status`: `WAITING`, `DETECTED`, `NOTIFIED`, and `EXPIRED`; fields are UUID primary key, task, display/normalized name, optional Maoyan cinema ID, booking URL, last match explanation, detected/notified/expired timestamps, and timestamps.
- `CheckRun.Status`: `SUCCEEDED`, `TEMPORARY_ERROR`, `RATE_LIMITED`, and `STRUCTURE_ERROR`; fields are UUID primary key, query key, start/finish timing, HTTP status, content fingerprint, cinema/detection counts, sanitized error code/message, and timestamps.
- `Notification.Type`: `OPENING`, `EXPIRY`, and `SYSTEM_ALERT`; `Notification.Status`: `PENDING`, `SENDING`, `SENT`, `FAILED`, `PERMANENT_FAILED`, and `CANCELLED`. Every notification has a non-null task, nullable task cinema for task-level expiry/system alerts, retry count/next attempt, deterministic Message-ID, sanitized SMTP response/error summaries, and timestamps.

Add these constraints:

```python
models.UniqueConstraint(
    fields=["task", "normalized_name"],
    name="uniq_task_cinema_name",
)
models.UniqueConstraint(
    fields=["task", "cinema_id"],
    name="uniq_task_nonempty_cinema_id",
    condition=~models.Q(cinema_id=""),
)
models.UniqueConstraint(
    fields=["user", "query_key", "cinema_set_hash"],
    name="uniq_active_user_query_cinemas",
    condition=models.Q(status__in=["MONITORING", "PARTIAL", "PAUSED", "ERROR"]),
)
```

and the two notification constraints:

```python
models.UniqueConstraint(
    fields=["task_cinema", "notification_type"],
    name="uniq_cinema_notification_type",
    condition=models.Q(task_cinema__isnull=False),
)
models.UniqueConstraint(
    fields=["task", "notification_type"],
    name="uniq_task_level_notification_type",
    condition=models.Q(task_cinema__isnull=True),
)
```

- [ ] **Step 3: Implement explicit state recomputation**

Create `monitoring/state.py`:

```python
from django.utils import timezone

from monitoring.models import MonitorTask, TaskCinema


def refresh_task_status(task):
    statuses = list(task.cinemas.values_list("status", flat=True))
    if not statuses:
        return task.status
    if all(value == TaskCinema.Status.NOTIFIED for value in statuses):
        task.status = MonitorTask.Status.COMPLETED
        task.completed_at = timezone.now()
    elif any(value == TaskCinema.Status.NOTIFIED for value in statuses):
        task.status = MonitorTask.Status.PARTIAL
    elif task.status not in {
        MonitorTask.Status.PAUSED,
        MonitorTask.Status.ERROR,
        MonitorTask.Status.COMPLETED,
        MonitorTask.Status.EXPIRED,
        MonitorTask.Status.CANCELLED,
    }:
        task.status = MonitorTask.Status.MONITORING
    task.save(update_fields=["status", "completed_at", "updated_at"])
    return task.status
```

- [ ] **Step 4: Migrate and verify**

Add `monitoring` to settings, then run:

```bash
.venv/bin/python manage.py makemigrations monitoring
.venv/bin/python manage.py migrate
.venv/bin/pytest monitoring/tests/test_state.py -v
.venv/bin/python manage.py check
```

Expected: state test passes and system check is clean.

- [ ] **Step 5: Commit**

```bash
git add monitoring ticketwatch
git commit -m "feat: add monitoring domain state"
```

### Task 6: Implement adapter contracts and safe Maoyan URL validation

**Files:**
- Create: `monitoring/adapters/base.py`
- Create: `monitoring/adapters/maoyan.py`
- Create: `monitoring/tests/test_maoyan_url.py`

**Interfaces:**
- Consumes: city ID/name selected by the server-owned city mapping.
- Produces: frozen `ParsedTarget`, `CinemaAvailability`, and `CheckResult` dataclasses; adapter exceptions; and `MaoyanAdapter.validate_target(url: str, city_id: int, city_name: str) -> ParsedTarget`.

- [ ] **Step 1: Write URL and SSRF tests**

Create `monitoring/tests/test_maoyan_url.py`:

```python
import pytest

from monitoring.adapters.base import TargetValidationError
from monitoring.adapters.maoyan import MaoyanAdapter


def test_valid_target_is_normalized():
    target = MaoyanAdapter().validate_target(
        "https://www.maoyan.com/cinemas?showDate=2026-08-20&brandId=357343&movieId=1545360",
        city_id=10,
        city_name="上海",
    )
    assert target.movie_id == "1545360"
    assert target.show_date.isoformat() == "2026-08-20"
    assert target.normalized_url.startswith("https://www.maoyan.com/cinemas?")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/cinemas?movieId=1&showDate=2026-08-20",
        "https://example.com/cinemas?movieId=1&showDate=2026-08-20",
        "file:///etc/passwd",
        "https://www.maoyan.com/films?movieId=1&showDate=2026-08-20",
        "https://www.maoyan.com/cinemas?showDate=2026-08-20",
    ],
)
def test_invalid_or_unsafe_target_is_rejected(url):
    with pytest.raises(TargetValidationError):
        MaoyanAdapter().validate_target(url, city_id=10, city_name="上海")
```

Run:

```bash
.venv/bin/pytest monitoring/tests/test_maoyan_url.py -v
```

Expected: FAIL because adapter classes do not exist.

- [ ] **Step 2: Define normalized adapter contracts**

Create `monitoring/adapters/base.py` with frozen dataclasses `ParsedTarget`, `CinemaAvailability`, and `CheckResult`. `CheckResult` must contain platform, city, movie, show date, normalized URL, valid-page flag, cinema tuple, content fingerprint, and optional sanitized error code. Define `TargetValidationError`, `TemporaryPlatformError`, `RateLimitedError`, and `PageStructureError`.

```python
from dataclasses import dataclass
from datetime import date


class TargetValidationError(ValueError):
    pass


class TemporaryPlatformError(RuntimeError):
    pass


class RateLimitedError(TemporaryPlatformError):
    pass


class PageStructureError(RuntimeError):
    pass


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
    platform: str
    city_id: int
    city_name: str
    movie_id: str
    movie_name: str
    show_date: date
    normalized_url: str
    valid_page: bool
    cinemas: tuple[CinemaAvailability, ...]
    content_fingerprint: str
    error_code: str = ""
```

- [ ] **Step 3: Implement strict target validation**

In `MaoyanAdapter.validate_target()`:

- allow only HTTPS;
- allow only exact lower-case hosts `www.maoyan.com` and `maoyan.com`;
- require path `/cinemas`;
- require one numeric `movieId` and one ISO `showDate`;
- reject user info, non-default ports, fragments, duplicate critical parameters, malformed dates, and past dates;
- retain only known filtering parameters;
- sort query parameters for a stable normalized URL;
- compute a query key from platform, city ID, and normalized URL.

Do not make a network request during validation.

```python
def validate_target(self, url, city_id, city_name):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"maoyan.com", "www.maoyan.com"}:
        raise TargetValidationError("only the Maoyan HTTPS host is allowed")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise TargetValidationError("userinfo and custom ports are forbidden")
    if parsed.path != "/cinemas" or parsed.fragment:
        raise TargetValidationError("target must be the Maoyan cinemas page")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if len(query.get("movieId", [])) != 1 or len(query.get("showDate", [])) != 1:
        raise TargetValidationError("one movieId and one showDate are required")
    movie_id = query["movieId"][0]
    if not movie_id.isdecimal():
        raise TargetValidationError("movieId must be numeric")
    try:
        show_date = date.fromisoformat(query["showDate"][0])
    except ValueError as exc:
        raise TargetValidationError("showDate must use YYYY-MM-DD") from exc
    if show_date < timezone.localdate():
        raise TargetValidationError("showDate cannot be in the past")
    allowed_keys = {"movieId", "showDate", "brandId", "districtId", "hallType", "serviceId"}
    if any(len(values) != 1 for key, values in query.items() if key in allowed_keys):
        raise TargetValidationError("filter parameters cannot be repeated")
    allowed = {key: values for key, values in query.items() if key in allowed_keys}
    normalized_url = urlunsplit(("https", "www.maoyan.com", "/cinemas", urlencode(sorted((k, v) for k, values in allowed.items() for v in values)), ""))
    query_key = f"maoyan:{city_id}:{sha256(normalized_url.encode()).hexdigest()}"
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
```

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/pytest monitoring/tests/test_maoyan_url.py -v
.venv/bin/ruff check monitoring/adapters monitoring/tests/test_maoyan_url.py
```

Expected: all URL tests pass and Ruff is clean.

- [ ] **Step 5: Commit**

```bash
git add monitoring/adapters monitoring/tests/test_maoyan_url.py
git commit -m "feat: validate maoyan monitoring targets"
```

### Task 7: Parse open, closed, captcha, and malformed Maoyan pages

**Files:**
- Create: `tests/fixtures/maoyan/open.html`
- Create: `tests/fixtures/maoyan/closed.html`
- Create: `tests/fixtures/maoyan/captcha.html`
- Create: `tests/fixtures/maoyan/malformed.html`
- Create: `monitoring/tests/test_maoyan_parser.py`
- Modify: `monitoring/adapters/maoyan.py`

**Interfaces:**
- Consumes: adapter dataclasses/exceptions and `ParsedTarget` from Task 6.
- Produces: `normalize_cinema_name(value: str) -> str`, `MaoyanAdapter.fetch(target: ParsedTarget) -> CheckResult`, and `MaoyanAdapter.parse_html(html: str, expected_city: str, expected_movie_id: str, expected_show_date: str, source_url: str) -> CheckResult`.

- [ ] **Step 1: Capture minimal sanitized fixtures**

Create fixtures containing only the structural elements needed by tests. `open.html` must contain selected city 上海, an active date link for 2026-08-20, one `.cinema-cell`, exact cinema name, and `/cinema/37534?...movieId=1545360` booking link. `closed.html` must contain the same city/date/query structure plus `.no-cinemas`. `captcha.html` must contain an access-verification marker. `malformed.html` must be HTTP-like HTML without the cinema-list structures.

```html
<!-- tests/fixtures/maoyan/open.html -->
<html><body data-city="上海" data-movie-id="1545360">
  <a class="date-item active" data-date="2026-08-20">8月20日</a>
  <div class="cinema-cell">
    <a class="cinema-name" href="/cinema/37534?movieId=1545360">MOViE MOViE 影城（前滩太古里店）</a>
    <a class="buy-btn" href="/cinema/37534?movieId=1545360&showDate=2026-08-20">选座购票</a>
  </div>
</body></html>
```

```html
<!-- tests/fixtures/maoyan/closed.html -->
<html><body data-city="上海" data-movie-id="1545360">
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
<html><body><main>Maoyan response without expected booking structures</main></body></html>
```

- [ ] **Step 2: Write parser tests**

Create `monitoring/tests/test_maoyan_parser.py`:

```python
from pathlib import Path

import pytest

from monitoring.adapters.base import PageStructureError, RateLimitedError
from monitoring.adapters.maoyan import MaoyanAdapter


FIXTURES = Path("tests/fixtures/maoyan")


def read_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_open_page_returns_exact_cinema_and_booking_link():
    result = MaoyanAdapter().parse_html(
        read_fixture("open.html"),
        expected_city="上海",
        expected_movie_id="1545360",
        expected_show_date="2026-08-20",
        source_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
    )
    assert result.valid_page is True
    assert result.cinemas[0].cinema_id == "37534"
    assert result.cinemas[0].name == "MOViE MOViE 影城（前滩太古里店）"
    assert result.cinemas[0].bookable is True


def test_closed_page_is_valid_with_no_cinemas():
    result = MaoyanAdapter().parse_html(
        read_fixture("closed.html"), "上海", "1545360", "2026-08-20", "https://www.maoyan.com/cinemas"
    )
    assert result.valid_page is True
    assert result.cinemas == ()


def test_captcha_is_not_treated_as_closed():
    with pytest.raises(RateLimitedError):
        MaoyanAdapter().parse_html(
            read_fixture("captcha.html"), "上海", "1545360", "2026-08-20", "https://www.maoyan.com/cinemas"
        )


def test_missing_structure_is_an_error():
    with pytest.raises(PageStructureError):
        MaoyanAdapter().parse_html(
            read_fixture("malformed.html"), "上海", "1545360", "2026-08-20", "https://www.maoyan.com/cinemas"
        )
```

Run:

```bash
.venv/bin/pytest monitoring/tests/test_maoyan_parser.py -v
```

Expected: FAIL because parsing is not implemented.

- [ ] **Step 3: Implement parsing and name normalization**

Implement `normalize_cinema_name()` using Unicode NFKC, case folding, bracket normalization, trimming, and whitespace collapsing. Parse only `.cinema-cell` cards; extract `.cinema-name`, the cinema ID from a `/cinema/<id>` link, and require link text containing `选座购票` for `bookable=True`. Validate selected city and active date/query metadata before returning a valid result. Hash response bytes with SHA-256 for `content_fingerprint`.

```python
BRACKETS = str.maketrans({"（": "(", "）": ")", "【": "[", "】": "]"})


def normalize_cinema_name(value):
    value = unicodedata.normalize("NFKC", value).translate(BRACKETS).casefold()
    return " ".join(value.split())


def _cinema_from_card(card, source_url):
    name_link = card.select_one("a.cinema-name")
    buy_link = next((link for link in card.select("a[href]") if "选座购票" in link.get_text(" ", strip=True)), None)
    if name_link is None:
        raise PageStructureError("cinema card is missing its name")
    href = buy_link.get("href", "") if buy_link else name_link.get("href", "")
    match = re.search(r"/cinema/(\d+)", href)
    if match is None:
        raise PageStructureError("cinema card is missing its platform id")
    name = name_link.get_text(" ", strip=True)
    return CinemaAvailability(match.group(1), name, normalize_cinema_name(name), buy_link is not None, urljoin(source_url, href))
```

- [ ] **Step 4: Implement safe fetching**

Implement `fetch()` with a `requests.Session`, current desktop User-Agent, 15-second timeout, `ci` Cookie, and `allow_redirects=False`. Reject all redirects rather than following user-influenced locations. Map 403/429 to `RateLimitedError`, 5xx and network errors to `TemporaryPlatformError`, and never convert exceptions into an empty cinema list.

```python
def fetch(self, target):
    try:
        response = self.session.get(
            target.normalized_url,
            headers={"User-Agent": self.USER_AGENT},
            cookies={"ci": str(target.city_id)},
            timeout=15,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise TemporaryPlatformError("Maoyan request failed") from exc
    if response.is_redirect:
        raise TemporaryPlatformError("Maoyan returned an unexpected redirect")
    if response.status_code in {403, 429}:
        raise RateLimitedError(f"Maoyan returned {response.status_code}")
    if response.status_code >= 500:
        raise TemporaryPlatformError(f"Maoyan returned {response.status_code}")
    response.raise_for_status()
    return self.parse_html(response.text, target.city_name, target.movie_id, target.show_date.isoformat(), target.normalized_url)
```

- [ ] **Step 5: Verify parser independence from live Maoyan**

Run:

```bash
.venv/bin/pytest monitoring/tests/test_maoyan_parser.py monitoring/tests/test_maoyan_url.py -v
.venv/bin/ruff check monitoring/adapters monitoring/tests
```

Expected: all adapter tests pass without internet access.

- [ ] **Step 6: Commit**

```bash
git add monitoring tests/fixtures/maoyan
git commit -m "feat: parse maoyan booking availability"
```

## Phase 3: Task creation, scheduling, and delivery

### Task 8: Build the preview-and-confirm task creation workflow

**Files:**
- Create: `monitoring/forms.py`
- Create: `monitoring/services.py`
- Create: `monitoring/views.py`
- Create: `monitoring/urls.py`
- Create: `monitoring/tests/test_task_creation.py`
- Modify: `ticketwatch/urls.py`

**Interfaces:**
- Consumes: `User`, verified `SMTPConfig`, `MaoyanAdapter.validate_target()`, `MaoyanAdapter.fetch()`, `normalize_cinema_name()`, and monitoring models.
- Produces: `TaskPreviewForm`, signed preview payload schema `TaskPreviewPayload`, `preview_target(city_id: int, city_name: str, source_url: str) -> TaskPreviewPayload`, `create_monitor_task(user: User, signed_preview: str, selected_ids: list[str], manual_names: list[str]) -> MonitorTask`, and `enqueue_initial_check(task_id: UUID) -> None`.

- [ ] **Step 1: Write failing creation tests**

Create tests for these four cases in `monitoring/tests/test_task_creation.py`:

```python
import pytest
from django.urls import reverse

from monitoring.models import MonitorTask, TaskCinema
from monitoring.services import PreviewCinema, TaskPreviewPayload, sign_preview


@pytest.mark.django_db
def test_preview_discovers_visible_cinemas(auth_client, mocker):
    mocker.patch(
        "monitoring.services.preview_target",
        return_value=TaskPreviewPayload(
            city_id=10,
            city_name="上海",
            source_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            normalized_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            query_key="maoyan:10:1545360:2026-08-20",
            movie_id="1545360",
            movie_name="奥德赛",
            show_date="2026-08-20",
            cinemas=(PreviewCinema(id="37534", name="MOViE MOViE 影城（前滩太古里店）"),),
        ),
    )
    response = auth_client.post(
        reverse("monitoring:task-preview"),
        {"city_name": "上海", "city_id": 10, "source_url": "https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20"},
    )
    assert response.status_code == 200
    assert "MOViE MOViE" in response.content.decode()


@pytest.mark.django_db
def test_confirm_accepts_selected_and_manual_cinemas(auth_client, mocker):
    mocker.patch("monitoring.services.enqueue_initial_check")
    signed_preview = sign_preview(
        TaskPreviewPayload(
            city_id=10,
            city_name="上海",
            source_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            normalized_url="https://www.maoyan.com/cinemas?movieId=1545360&showDate=2026-08-20",
            query_key="maoyan:10:1545360:2026-08-20",
            movie_id="1545360",
            movie_name="奥德赛",
            show_date="2026-08-20",
            cinemas=(PreviewCinema(id="37534", name="MOViE MOViE 影城（前滩太古里店）"),),
        )
    )
    response = auth_client.post(
        reverse("monitoring:task-confirm"),
        {
            "signed_preview": signed_preview,
            "selected_cinemas": ["37534"],
            "manual_cinemas": "未来影院\n",
        },
    )
    assert response.status_code == 302
    task = MonitorTask.objects.get()
    assert set(task.cinemas.values_list("display_name", flat=True)) == {
        "MOViE MOViE 影城（前滩太古里店）",
        "未来影院",
    }
    assert task.cinemas.get(display_name="未来影院").cinema_id == ""


@pytest.mark.django_db
def test_task_creation_requires_verified_smtp(auth_client):
    response = auth_client.post(reverse("monitoring:task-confirm"), {"city_name": "上海"})
    assert response.status_code == 403


@pytest.mark.django_db
def test_users_cannot_view_each_others_tasks(auth_client, other_users_task):
    response = auth_client.get(reverse("monitoring:task-detail", args=[other_users_task.pk]))
    assert response.status_code == 404
```

Run:

```bash
.venv/bin/pytest monitoring/tests/test_task_creation.py -v
```

Expected: FAIL because forms, services, URLs, and views are absent.

- [ ] **Step 2: Implement validated preview data**

`TaskPreviewForm` accepts only a city from the server-owned Maoyan city mapping and a URL accepted by `MaoyanAdapter.validate_target()`. Define the signed value types exactly:

```python
from dataclasses import asdict, dataclass

from django.core import signing


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


def sign_preview(payload: TaskPreviewPayload) -> str:
    return signing.dumps(asdict(payload), salt="task-preview", compress=True)


def unsign_preview(value: str) -> dict:
    return signing.loads(value, salt="task-preview", max_age=30 * 60)
```

The preview view performs one safe fetch, renders the normalized movie/date and currently visible cinemas, and signs this normalized payload. Confirmation must not trust hidden movie, date, city, or cinema metadata supplied directly by the browser.

- [ ] **Step 3: Implement atomic confirmation**

Create `create_monitor_task(user, signed_preview, selected_ids, manual_names)` with `transaction.atomic()`. Require the user's `SMTPConfig.is_verified`; require every selected ID to exist in the signed payload; deduplicate normalized manual and selected names; reject zero or more than 20 cinemas; derive a stable cinema-set hash; rely on the conditional database uniqueness constraint to reject a duplicate active task; create `MonitorTask` and `TaskCinema` rows; write an audit event without the full query string; then register `enqueue_initial_check(task.pk)` with `transaction.on_commit()`:

```python
from hashlib import sha256

from celery import current_app
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction


def create_monitor_task(user, signed_preview, selected_ids, manual_names):
    if not SMTPConfig.objects.filter(user=user, is_verified=True).exists():
        raise PermissionDenied("verified SMTP configuration required")
    payload = unsign_preview(signed_preview)
    visible = {item["id"]: item["name"] for item in payload["cinemas"]}
    if any(cinema_id not in visible for cinema_id in selected_ids):
        raise ValidationError("selected cinema was not present in the signed preview")
    requested = [(cinema_id, visible[cinema_id]) for cinema_id in selected_ids]
    requested.extend(("", value) for value in manual_names if value.strip())
    deduplicated = {
        normalize_cinema_name(name): (cinema_id, name.strip())
        for cinema_id, name in requested
    }
    if not 1 <= len(deduplicated) <= 20:
        raise ValidationError("choose between 1 and 20 cinemas")
    cinema_set_hash = sha256("\n".join(sorted(deduplicated)).encode("utf-8")).hexdigest()
    with transaction.atomic():
        try:
            task = MonitorTask.objects.create(
                user=user,
                city_id=payload["city_id"],
                city_name=payload["city_name"],
                normalized_url=payload["normalized_url"],
                source_url=payload["source_url"],
                movie_id=payload["movie_id"],
                movie_name=payload["movie_name"],
                show_date=payload["show_date"],
                query_key=payload["query_key"],
                cinema_set_hash=cinema_set_hash,
                status=MonitorTask.Status.MONITORING,
            )
        except IntegrityError as exc:
            raise ValidationError("an active task already monitors this cinema set") from exc
        TaskCinema.objects.bulk_create(
            TaskCinema(
                task=task,
                cinema_id=cinema_id,
                display_name=name,
                normalized_name=normalized,
            )
            for normalized, (cinema_id, name) in deduplicated.items()
        )
        record_event(user, "monitor.task.created", task, {"cinema_count": len(deduplicated)})
        transaction.on_commit(lambda: enqueue_initial_check(task.pk))
    return task


def enqueue_initial_check(task_id):
    query_key = MonitorTask.objects.values_list("query_key", flat=True).get(pk=task_id)
    current_app.send_task(
        "monitoring.tasks.check_query_group",
        args=[query_key],
        queue="checks",
    )
```

Selected cinemas retain their platform ID. Manual cinemas have an empty ID until a later exact normalized-name match supplies one. Reject duplicate active tasks for the same user/query/cinema set with a form error rather than silently creating another monitor.

- [ ] **Step 4: Implement owner-scoped views**

All list/detail/pause/resume/cancel querysets begin with `MonitorTask.objects.filter(user=request.user)`. Use POST plus CSRF protection for every state-changing route. Do not accept a user ID in a form or URL.

```python
class OwnedTaskMixin(LoginRequiredMixin):
    model = MonitorTask

    def get_queryset(self):
        return MonitorTask.objects.filter(user=self.request.user).prefetch_related("cinemas")


@login_required
@require_POST
def pause_task(request, pk):
    task = get_object_or_404(MonitorTask.objects.filter(user=request.user), pk=pk)
    task.status = MonitorTask.Status.PAUSED
    task.save(update_fields=["status", "updated_at"])
    record_event(request.user, "monitor.task.paused", task)
    return redirect("monitoring:task-detail", pk=task.pk)
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest monitoring/tests/test_task_creation.py -v
.venv/bin/ruff check monitoring
git add monitoring ticketwatch
git commit -m "feat: add monitored task creation workflow"
```

Expected: all creation and isolation tests pass; Ruff is clean.

### Task 9: Add grouped adaptive polling with a hard 60-second floor

**Files:**
- Create: `ticketwatch/celery.py`
- Create: `monitoring/polling.py`
- Create: `monitoring/tasks.py`
- Create: `monitoring/tests/test_polling.py`
- Modify: `ticketwatch/__init__.py`
- Modify: `ticketwatch/settings.py`

**Interfaces:**
- Consumes: `MonitorTask.query_key`, `MonitorTask.next_check_at`, administrator-owned `SystemPolicy` interval values, `MaoyanAdapter.fetch()`, and `CheckRun`.
- Produces: `MIN_INTERVAL_SECONDS = 60`, frozen `PollingIntervals`, `DEFAULT_INTERVALS`, `FAILURE_BACKOFF_SECONDS = (120, 300, 900, 1800, 3600)`, `calculate_next_check(now: datetime, show_date: date, consecutive_failures: int, intervals: PollingIntervals = DEFAULT_INTERVALS, jitter_seconds: int | None = None) -> datetime`, `due_query_groups(now: datetime) -> list[str]`, Celery task `dispatch_due_checks() -> int`, and Celery task `check_query_group(query_key: str) -> None`.

- [ ] **Step 1: Write failing scheduler and grouping tests**

```python
from datetime import timedelta

import pytest
from django.utils import timezone

from monitoring.polling import MIN_INTERVAL_SECONDS, calculate_next_check, due_query_groups


def test_interval_never_falls_below_one_minute():
    for failures in range(8):
        next_at = calculate_next_check(
            now=timezone.now(),
            show_date=timezone.localdate() + timedelta(days=1),
            consecutive_failures=failures,
            jitter_seconds=0,
        )
        assert (next_at - timezone.now()).total_seconds() >= MIN_INTERVAL_SECONDS - 1


@pytest.mark.django_db
def test_identical_queries_are_fetched_once(task_factory, mocker):
    first = task_factory(query_key="maoyan:10:1545360:2026-08-20")
    second = task_factory(query_key=first.query_key)
    fetch = mocker.patch("monitoring.tasks.MaoyanAdapter.fetch")
    fetch.return_value.cinemas = ()
    due_query_groups(now=timezone.now())
    from monitoring.tasks import check_query_group
    check_query_group(first.query_key)
    fetch.assert_called_once()
    assert second.query_key == first.query_key


def test_failures_back_off_without_marking_closed():
    normal = calculate_next_check(timezone.now(), timezone.localdate(), 0, jitter_seconds=0)
    failed = calculate_next_check(timezone.now(), timezone.localdate(), 4, jitter_seconds=0)
    assert failed > normal


def test_custom_intervals_are_used_but_cannot_cross_floor():
    intervals = PollingIntervals(near_seconds=120, medium_seconds=420, far_seconds=1200)
    next_at = calculate_next_check(
        timezone.now(), timezone.localdate(), 0, intervals=intervals, jitter_seconds=0
    )
    assert (next_at - timezone.now()).total_seconds() >= 119
    with pytest.raises(ValueError, match="at least 60"):
        PollingIntervals(near_seconds=59, medium_seconds=300, far_seconds=900)
```

Run ` .venv/bin/pytest monitoring/tests/test_polling.py -v` and expect failures.

- [ ] **Step 2: Configure Celery explicitly**

Create the conventional Django Celery app, load configuration from settings with the `CELERY_` namespace, and autodiscover tasks. Configure JSON-only serialization, UTC transport with `CELERY_TIMEZONE = "Asia/Shanghai"`, task acknowledgements after execution, worker lost rejection, Redis visibility timeout, and separate `checks` and `mail` queues. Beat scans due database rows every 30 seconds, while persisted `next_check_at`, grouped dispatch, and the Redis lock guarantee that no platform query is executed more frequently than once per 60 seconds.

```python
# ticketwatch/celery.py
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ticketwatch.settings")
app = Celery("ticketwatch")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

```python
# ticketwatch/settings.py additions
CELERY_BROKER_URL = os.environ["REDIS_URL"]
CELERY_RESULT_BACKEND = os.environ["REDIS_URL"]
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Asia/Shanghai"
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_TASK_ROUTES = {
    "monitoring.tasks.check_query_group": {"queue": "checks"},
    "monitoring.tasks.dispatch_due_notifications": {"queue": "mail"},
    "monitoring.tasks.send_opening_notification": {"queue": "mail"},
    "monitoring.tasks.send_expiry_notification": {"queue": "mail"},
    "monitoring.tasks.expire_due_tasks": {"queue": "checks"},
}
CELERY_BEAT_SCHEDULE = {
    "dispatch-due-checks": {"task": "monitoring.tasks.dispatch_due_checks", "schedule": 30.0},
    "dispatch-due-notifications": {"task": "monitoring.tasks.dispatch_due_notifications", "schedule": 60.0},
    "expire-due-tasks": {"task": "monitoring.tasks.expire_due_tasks", "schedule": 300.0},
}
```

- [ ] **Step 3: Implement deterministic adaptive scheduling**

Set `MIN_INTERVAL_SECONDS = 60`. Defaults are: show date today or tomorrow is 60 seconds; 2–7 days away is 300 seconds; more than 7 days away is 900 seconds. Represent them as a validated value object so Task 13 can supply administrator-edited values on every scheduling decision. On consecutive network/5xx failures use 2, 5, 15, 30, then 60 minutes. Clamp jitter so the final result is never below 60 seconds:

```python
import random
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone


MIN_INTERVAL_SECONDS = 60
FAILURE_BACKOFF_SECONDS = (120, 300, 900, 1800, 3600)


@dataclass(frozen=True)
class PollingIntervals:
    near_seconds: int = 60
    medium_seconds: int = 300
    far_seconds: int = 900

    def __post_init__(self):
        if min(self.near_seconds, self.medium_seconds, self.far_seconds) < MIN_INTERVAL_SECONDS:
            raise ValueError("all polling intervals must be at least 60 seconds")


DEFAULT_INTERVALS = PollingIntervals()


def calculate_next_check(
    now,
    show_date,
    consecutive_failures,
    intervals=DEFAULT_INTERVALS,
    jitter_seconds=None,
):
    days = (show_date - timezone.localdate(now)).days
    if consecutive_failures:
        base = FAILURE_BACKOFF_SECONDS[min(consecutive_failures - 1, len(FAILURE_BACKOFF_SECONDS) - 1)]
    elif days <= 1:
        base = intervals.near_seconds
    elif days <= 7:
        base = intervals.medium_seconds
    else:
        base = intervals.far_seconds
    jitter = random.randint(0, min(30, base // 10)) if jitter_seconds is None else jitter_seconds
    effective = max(MIN_INTERVAL_SECONDS, base + max(0, jitter))
    return now + timedelta(seconds=effective)
```

Persist `next_check_at` in PostgreSQL; Redis is only a lock/broker and must not own schedule truth.

- [ ] **Step 4: Group and lock due queries**

`dispatch_due_checks` selects due monitoring/partial tasks grouped by `query_key` and enqueues one `check_query_group` per group. The database-facing selector is exact:

```python
def due_query_groups(now):
    return list(
        MonitorTask.objects.filter(
            status__in=[MonitorTask.Status.MONITORING, MonitorTask.Status.PARTIAL],
            next_check_at__lte=now,
        )
        .order_by()
        .values_list("query_key", flat=True)
        .distinct()
    )


@shared_task(queue="checks")
def dispatch_due_checks():
    keys = due_query_groups(timezone.now())
    for query_key in keys:
        check_query_group.delay(query_key)
    return len(keys)
```

`check_query_group` acquires `cache.add(f"check-lock:{sha256(query_key)}", token, timeout=55)`; a Lua compare-and-delete releases only the caller's token. Inside one group, fetch once and apply the normalized result to every still-active task in a database transaction. A failed fetch creates a failed `CheckRun`, increments failure counters, schedules backoff, and never changes a cinema to detected/notified. Temporary/rate-limit failures reaching the configured threshold place the query's active tasks in `ERROR`; a successful valid fetch resets both fetch and structure counters.

- [ ] **Step 5: Verify Redis-loss recovery and commit**

Add a test that clears the cache between dispatches and proves the same persisted task becomes due again without data loss. Then run:

```bash
.venv/bin/pytest monitoring/tests/test_polling.py monitoring/tests/test_state.py -v
.venv/bin/ruff check ticketwatch monitoring
git add ticketwatch monitoring
git commit -m "feat: add grouped adaptive polling"
```

Expected: scheduling, grouping, locking, and recovery tests pass.

### Task 10: Detect exact cinemas and deliver idempotent notifications

**Files:**
- Modify: `monitoring/services.py`
- Modify: `monitoring/tasks.py`
- Modify: `mailsettings/services.py`
- Create: `monitoring/tests/test_detection.py`
- Create: `monitoring/tests/test_notifications.py`

**Interfaces:**
- Consumes: `CheckResult`, exact-ID/name matching rules, `Notification`, `send_user_message()`, and `refresh_task_status()`.
- Produces: `apply_check_result(query_key: str, result: CheckResult) -> int` returning newly detected cinema count; `build_opening_message(notification: Notification) -> EmailMessage`; `reschedule_notification(notification: Notification, error: str, retry_minutes: tuple[int, ...], delivery_task: Task) -> None`; Celery task `dispatch_due_notifications() -> int`; and Celery task `send_opening_notification(notification_id: UUID) -> None`.

- [ ] **Step 1: Write failing detection tests**

Cover these cases:

```python
@pytest.mark.django_db
def test_manual_cinema_requires_exact_normalized_name(task_factory, parsed_result_factory):
    task = task_factory(cinema_names=["MOViE MOViE 影城（前滩太古里店）"])
    near_match = parsed_result_factory(cinema_name="MOViE MOViE 影城（前滩店）", bookable=True)
    apply_check_result(task.query_key, near_match)
    assert task.cinemas.get().status == TaskCinema.Status.WAITING


@pytest.mark.django_db
def test_creation_time_opening_is_detected_and_enqueued_once(task_factory, parsed_result_factory, mocker):
    task = task_factory(cinema_names=["MOViE MOViE 影城（前滩太古里店）"])
    send = mocker.patch("monitoring.tasks.send_opening_notification.delay")
    result = parsed_result_factory(cinema_name=task.cinemas.get().display_name, bookable=True)
    apply_check_result(task.query_key, result)
    apply_check_result(task.query_key, result)
    assert send.call_count == 1
```

Also test that platform cinema ID takes precedence over name, a valid empty page stays waiting, and captcha/malformed results cannot produce detection.

- [ ] **Step 2: Apply detections transactionally**

Lock candidate `TaskCinema` rows with `select_for_update()`. Match a non-empty platform cinema ID first; otherwise require equality of normalized names. On the first bookable match, set `status=DETECTED`, `detected_at`, and booking URL, then `get_or_create` one opening `Notification`. Register its Celery delivery only on transaction commit. Reapplying the same page must make no additional notification row or queue call.

```python
def apply_check_result(query_key, result):
    if not result.valid_page:
        raise PageStructureError(result.error_code or "invalid page")
    by_id = {item.cinema_id: item for item in result.cinemas if item.bookable and item.cinema_id}
    by_name = {item.normalized_name: item for item in result.cinemas if item.bookable}
    created_ids = []
    with transaction.atomic():
        cinemas = TaskCinema.objects.select_for_update().filter(
            task__query_key=query_key,
            task__status__in=[MonitorTask.Status.MONITORING, MonitorTask.Status.PARTIAL],
            status=TaskCinema.Status.WAITING,
        )
        for cinema in cinemas:
            match = by_id.get(cinema.cinema_id) if cinema.cinema_id else by_name.get(cinema.normalized_name)
            if match is None:
                continue
            cinema.status = TaskCinema.Status.DETECTED
            cinema.cinema_id = match.cinema_id
            cinema.booking_url = match.booking_url
            cinema.detected_at = timezone.now()
            cinema.save(update_fields=["status", "cinema_id", "booking_url", "detected_at", "updated_at"])
            notification, created = Notification.objects.get_or_create(
                task=cinema.task,
                task_cinema=cinema,
                notification_type=Notification.Type.OPENING,
                defaults={"status": Notification.Status.PENDING},
            )
            if created:
                created_ids.append(notification.pk)
        for notification_id in created_ids:
            transaction.on_commit(lambda value=notification_id: send_opening_notification.delay(value))
    return len(created_ids)
```

- [ ] **Step 3: Write failing mail-delivery tests**

Test that two deliveries cannot both claim one notification, a success sets cinema `NOTIFIED`, a temporary SMTP error returns notification to `PENDING` with retry metadata, Redis/cache clearing still allows the PostgreSQL-backed dispatcher to re-enqueue it, and an authentication error sets `SMTPConfig.is_verified=False`, marks notification `PERMANENT_FAILED`, and surfaces an actionable task/admin error. A worker-lost `SENDING` row is surfaced to operators and is not automatically replayed because SMTP acceptance may already have occurred.

```python
@pytest.mark.django_db(transaction=True)
def test_notification_is_claimed_once(notification_factory, mocker):
    notification = notification_factory()
    send = mocker.patch("monitoring.tasks.send_user_message", return_value="<accepted@ticketwatch>")
    send_opening_notification(notification.pk)
    send_opening_notification(notification.pk)
    assert send.call_count == 1


@pytest.mark.django_db
def test_success_marks_cinema_notified(notification_factory, mocker):
    notification = notification_factory()
    mocker.patch("monitoring.tasks.send_user_message", return_value="<accepted@ticketwatch>")
    send_opening_notification(notification.pk)
    notification.refresh_from_db()
    notification.task_cinema.refresh_from_db()
    assert notification.status == Notification.Status.SENT
    assert notification.task_cinema.status == TaskCinema.Status.NOTIFIED


@pytest.mark.django_db
def test_authentication_failure_disables_smtp(notification_factory, mocker):
    notification = notification_factory()
    mocker.patch("monitoring.tasks.send_user_message", side_effect=smtplib.SMTPAuthenticationError(535, b"denied"))
    send_opening_notification(notification.pk)
    notification.refresh_from_db()
    notification.task.user.smtp_config.refresh_from_db()
    assert notification.status == Notification.Status.PERMANENT_FAILED
    assert notification.task.user.smtp_config.is_verified is False
```

- [ ] **Step 4: Implement notification claiming and SMTP delivery**

Claim with `select_for_update(skip_locked=True)` and an atomic `PENDING -> SENDING` transition. Build a deterministic Message-ID from the notification UUID and application hostname. The opening email contains exactly: movie, requested date, city, cinema, detection time in Asia/Shanghai, validated original monitoring URL, and sanitized booking link. It contains no price or showtime. Never log recipient credentials, decrypted authorization code, Cookie, session, invitation token, or complete email body.

After SMTP acceptance, atomically mark the notification `SENT` and cinema `NOTIFIED`, then call `refresh_task_status()`. Retry network/4xx SMTP errors with 1, 5, 15, 30, then 60 minutes; cap at five retry attempts. Do not retry authentication or invalid-recipient errors until the user changes settings.

```python
RETRY_MINUTES = (1, 5, 15, 30, 60)


def build_opening_message(notification):
    task = notification.task
    cinema = notification.task_cinema
    detected_at = timezone.localtime(cinema.detected_at).strftime("%Y-%m-%d %H:%M:%S %Z")
    message = EmailMessage()
    message["Subject"] = f"[TicketWatch] {task.movie_name} 已在 {cinema.display_name} 开票"
    message["From"] = task.user.smtp_config.from_email
    message["To"] = task.user.email
    message["Message-ID"] = notification.message_id
    message.set_content(
        "\n".join(
            [
                f"电影：{task.movie_name}",
                f"日期：{task.show_date.isoformat()}",
                f"城市：{task.city_name}",
                f"影院：{cinema.display_name}",
                f"发现时间：{detected_at}",
                f"监控链接：{task.source_url}",
                f"购票链接：{cinema.booking_url}",
            ]
        )
    )
    return message


def fail_authentication(notification, error):
    with transaction.atomic():
        current = Notification.objects.select_for_update().select_related("task__user__smtp_config").get(pk=notification.pk)
        current.status = Notification.Status.PERMANENT_FAILED
        current.last_error = error
        current.save(update_fields=["status", "last_error", "updated_at"])
        config = current.task.user.smtp_config
        config.is_verified = False
        config.last_error = error
        config.save(update_fields=["is_verified", "last_error", "updated_at"])
        current.task.last_error = "SMTP authentication failed; verify your mail settings"
        current.task.save(update_fields=["last_error", "updated_at"])


def fail_permanently(notification, error):
    Notification.objects.filter(pk=notification.pk, status=Notification.Status.SENDING).update(
        status=Notification.Status.PERMANENT_FAILED,
        last_error=error,
        updated_at=timezone.now(),
    )


def reschedule_notification(notification, error, retry_minutes, delivery_task):
    with transaction.atomic():
        current = Notification.objects.select_for_update().get(pk=notification.pk)
        if current.retry_count >= len(retry_minutes):
            current.status = Notification.Status.FAILED
            current.last_error = error
            current.save(update_fields=["status", "last_error", "updated_at"])
            return
        delay = retry_minutes[current.retry_count]
        current.retry_count += 1
        current.status = Notification.Status.PENDING
        current.last_error = error
        current.next_attempt_at = timezone.now() + timedelta(minutes=delay)
        current.save(
            update_fields=["retry_count", "status", "last_error", "next_attempt_at", "updated_at"]
        )
        transaction.on_commit(
            lambda: delivery_task.apply_async(args=[current.pk], eta=current.next_attempt_at)
        )


@shared_task(queue="mail")
def dispatch_due_notifications():
    due = list(
        Notification.objects.filter(status=Notification.Status.PENDING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now()))
        .order_by("created_at")
        .values_list("pk", "notification_type")[:500]
    )
    for notification_id, notification_type in due:
        if notification_type == Notification.Type.EXPIRY:
            delivery = send_expiry_notification
        elif notification_type == Notification.Type.OPENING:
            delivery = send_opening_notification
        else:
            continue
        delivery.delay(notification_id)
    return len(due)


@shared_task(queue="mail")
def send_opening_notification(notification_id):
    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update(skip_locked=True)
            .select_related("task", "task__user", "task_cinema", "task__user__smtp_config")
            .filter(pk=notification_id, status=Notification.Status.PENDING)
            .first()
        )
        if notification is None:
            return
        notification.status = Notification.Status.SENDING
        notification.message_id = f"<opening-{notification.pk}@{settings.MESSAGE_ID_DOMAIN}>"
        notification.save(update_fields=["status", "message_id", "updated_at"])
    try:
        accepted_id = send_user_message(notification.task.user.smtp_config, build_opening_message(notification))
    except smtplib.SMTPAuthenticationError as exc:
        fail_authentication(notification, sanitize_smtp_error(exc))
        return
    except smtplib.SMTPRecipientsRefused as exc:
        fail_permanently(notification, sanitize_smtp_error(exc))
        return
    except (OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPDataError) as exc:
        reschedule_notification(notification, sanitize_smtp_error(exc), RETRY_MINUTES, send_opening_notification)
        return
    with transaction.atomic():
        notification = Notification.objects.select_for_update().get(pk=notification_id)
        notification.status = Notification.Status.SENT
        notification.smtp_response = accepted_id
        notification.sent_at = timezone.now()
        notification.save(update_fields=["status", "smtp_response", "sent_at", "updated_at"])
        cinema = notification.task_cinema
        cinema.status = TaskCinema.Status.NOTIFIED
        cinema.notified_at = timezone.now()
        cinema.save(update_fields=["status", "notified_at", "updated_at"])
        refresh_task_status(notification.task)
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest monitoring/tests/test_detection.py monitoring/tests/test_notifications.py -v
.venv/bin/ruff check monitoring mailsettings
git add monitoring mailsettings
git commit -m "feat: detect openings and send idempotent alerts"
```

Expected: exact matching, immediate detection, idempotency, and retry tests pass.

### Task 11: Expire unfinished tasks at the requested date boundary

**Files:**
- Modify: `monitoring/tasks.py`
- Modify: `monitoring/state.py`
- Create: `monitoring/tests/test_expiry.py`
- Modify: `ticketwatch/settings.py`

**Interfaces:**
- Consumes: monitoring lifecycle models, `send_user_message()`, notification failure/retry helpers from Task 10, the mail queue, and Asia/Shanghai local date.
- Produces: task-level expiry notification uniqueness, `build_expiry_message(notification: Notification) -> EmailMessage`, Celery task `send_expiry_notification(notification_id: UUID) -> None`, and Celery task `expire_due_tasks() -> int` returning the number of newly expired tasks.

- [ ] **Step 1: Write failing expiry tests**

At `2026-08-21 00:00:00 Asia/Shanghai`, prove an unfinished task for `2026-08-20` becomes `EXPIRED`, waiting cinemas become `EXPIRED`, one task-level expiry notification is created, and later beat runs do not create duplicates. Prove completed/cancelled tasks are unchanged and the exact end boundary is timezone-aware.

```python
@pytest.mark.django_db
@freeze_time("2026-08-21 00:00:00", tz_offset=8)
def test_expiry_is_timezone_aware_and_idempotent(task_factory):
    task = task_factory(show_date=date(2026, 8, 20), cinema_names=["影院甲", "影院乙"])
    assert expire_due_tasks() == 1
    assert expire_due_tasks() == 0
    task.refresh_from_db()
    assert task.status == MonitorTask.Status.EXPIRED
    assert set(task.cinemas.values_list("status", flat=True)) == {TaskCinema.Status.EXPIRED}
    assert Notification.objects.filter(
        task=task,
        task_cinema__isnull=True,
        notification_type=Notification.Type.EXPIRY,
    ).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("status", [MonitorTask.Status.COMPLETED, MonitorTask.Status.CANCELLED])
def test_terminal_tasks_do_not_expire(task_factory, status):
    task = task_factory(show_date=date(2020, 1, 1), status=status)
    expire_due_tasks()
    task.refresh_from_db()
    assert task.status == status
```

- [ ] **Step 2: Implement periodic expiry**

Run `expire_due_tasks` from beat every five minutes. In an atomic transaction, lock tasks whose local `show_date < timezone.localdate()` and status is monitoring/partial/paused/error. Cancel any not-yet-claimed opening notification, set task/cinema expiry fields, and create one expiry notification protected by a database unique constraint on `(task, notification_type)`. The expiry email lists movie, requested date, city, cinemas that were not successfully notified (including detected openings whose mail never succeeded), expiry time, and source link.

```python
def build_expiry_message(notification):
    task = notification.task
    cinema_names = list(
        task.cinemas.filter(status=TaskCinema.Status.EXPIRED)
        .order_by("display_name")
        .values_list("display_name", flat=True)
    )
    expired_at = timezone.localtime(task.expired_at).strftime("%Y-%m-%d %H:%M:%S %Z")
    message = EmailMessage()
    message["Subject"] = f"[TicketWatch] {task.movie_name} 监控已到期"
    message["From"] = task.user.smtp_config.from_email
    message["To"] = task.user.email
    message["Message-ID"] = notification.message_id
    message.set_content(
        "\n".join(
            [
                f"电影：{task.movie_name}",
                f"日期：{task.show_date.isoformat()}",
                f"城市：{task.city_name}",
                f"未开票影院：{'、'.join(cinema_names)}",
                f"到期时间：{expired_at}",
                f"来源链接：{task.source_url}",
            ]
        )
    )
    return message


@shared_task(queue="mail")
def send_expiry_notification(notification_id):
    with transaction.atomic():
        notification = (
            Notification.objects.select_for_update(skip_locked=True)
            .select_related("task", "task__user", "task__user__smtp_config")
            .filter(
                pk=notification_id,
                notification_type=Notification.Type.EXPIRY,
                status=Notification.Status.PENDING,
            )
            .first()
        )
        if notification is None:
            return
        notification.status = Notification.Status.SENDING
        notification.message_id = f"<expiry-{notification.pk}@{settings.MESSAGE_ID_DOMAIN}>"
        notification.save(update_fields=["status", "message_id", "updated_at"])
    try:
        accepted_id = send_user_message(notification.task.user.smtp_config, build_expiry_message(notification))
    except smtplib.SMTPAuthenticationError as exc:
        fail_authentication(notification, sanitize_smtp_error(exc))
        return
    except smtplib.SMTPRecipientsRefused as exc:
        fail_permanently(notification, sanitize_smtp_error(exc))
        return
    except (OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPDataError) as exc:
        reschedule_notification(notification, sanitize_smtp_error(exc), RETRY_MINUTES, send_expiry_notification)
        return
    Notification.objects.filter(pk=notification_id, status=Notification.Status.SENDING).update(
        status=Notification.Status.SENT,
        smtp_response=accepted_id,
        sent_at=timezone.now(),
        updated_at=timezone.now(),
    )


@shared_task(queue="checks")
def expire_due_tasks():
    expired_ids = []
    with transaction.atomic():
        tasks = MonitorTask.objects.select_for_update(skip_locked=True).filter(
            show_date__lt=timezone.localdate(),
            status__in=[
                MonitorTask.Status.MONITORING,
                MonitorTask.Status.PARTIAL,
                MonitorTask.Status.PAUSED,
                MonitorTask.Status.ERROR,
            ],
        )
        for task in tasks:
            now = timezone.now()
            task.status = MonitorTask.Status.EXPIRED
            task.expired_at = now
            task.save(update_fields=["status", "expired_at", "updated_at"])
            Notification.objects.filter(
                task=task,
                notification_type=Notification.Type.OPENING,
                status=Notification.Status.PENDING,
            ).update(status=Notification.Status.CANCELLED, updated_at=now)
            task.cinemas.filter(status__in=[TaskCinema.Status.WAITING, TaskCinema.Status.DETECTED]).update(
                status=TaskCinema.Status.EXPIRED,
                expired_at=now,
                updated_at=now,
            )
            notification, created = Notification.objects.get_or_create(
                task=task,
                task_cinema=None,
                notification_type=Notification.Type.EXPIRY,
                defaults={"status": Notification.Status.PENDING},
            )
            if created:
                expired_ids.append(notification.pk)
        for notification_id in expired_ids:
            transaction.on_commit(lambda value=notification_id: send_expiry_notification.delay(value))
    return len(expired_ids)
```

- [ ] **Step 3: Verify and commit**

```bash
.venv/bin/pytest monitoring/tests/test_expiry.py -v
.venv/bin/python manage.py check
git add monitoring ticketwatch
git commit -m "feat: expire unfinished monitoring tasks"
```

Expected: expiry boundary and idempotency tests pass.

## Phase 4: Web experience, administration, and security

### Task 12: Add the invite-only user interface

**Files:**
- Create: `templates/base.html`
- Create: `templates/registration/login.html`
- Create: `templates/registration/password_change_form.html`
- Create: `templates/registration/password_change_done.html`
- Create: `templates/accounts/invitation_form.html`
- Create: `templates/accounts/invitation_accept.html`
- Create: `templates/mailsettings/config_form.html`
- Create: `templates/monitoring/task_list.html`
- Create: `templates/monitoring/task_preview.html`
- Create: `templates/monitoring/task_detail.html`
- Create: `static/css/app.css`
- Create: `accounts/rate_limits.py`
- Modify: `accounts/forms.py`
- Modify: `accounts/views.py`
- Modify: `mailsettings/views.py`
- Modify: `monitoring/views.py`
- Create: `monitoring/tests/test_web.py`

**Interfaces:**
- Consumes: invitation, authentication, SMTP, task creation, owner-scoped lifecycle services from Tasks 3–11, Django cache, and the Nginx-overwritten real client IP header.
- Produces: named URL routes under `accounts`, `mailsettings`, and `monitoring`; Bootstrap 5 server-rendered templates; `enforce_rate_limit(request, scope: str, identity: str, limit: int, window_seconds: int) -> None`; and POST-only pause/resume/cancel controls.

- [ ] **Step 1: Write failing browser-level Django tests**

Test login/logout/password change, no registration URL, invitation acceptance, SMTP password never rendered back, task list and status filters scoped to owner, task-detail check/mail history scoped to owner, preview selection plus manual cinema rows, pause/resume/cancel POST behavior, CSRF middleware enabled, accessible labels/error summaries, and cache-backed login/invitation throttling returning HTTP 429 after five failures per 15 minutes.

```python
@pytest.mark.django_db
def test_there_is_no_public_registration_route(client):
    assert client.get("/accounts/register/").status_code == 404


@pytest.mark.django_db
def test_smtp_secret_is_never_rendered(auth_client, smtp_config):
    smtp_config.set_password("authorization-code")
    smtp_config.save()
    body = auth_client.get(reverse("mailsettings:edit")).content.decode()
    assert "authorization-code" not in body
    assert smtp_config.encrypted_password not in body


@pytest.mark.django_db
def test_task_list_is_owner_scoped(auth_client, users_task, other_users_task):
    body = auth_client.get(reverse("monitoring:task-list")).content.decode()
    assert str(users_task.pk) in body
    assert str(other_users_task.pk) not in body


@pytest.mark.django_db
def test_pause_rejects_get(auth_client, users_task):
    assert auth_client.get(reverse("monitoring:task-pause", args=[users_task.pk])).status_code == 405


@pytest.mark.django_db
def test_login_failures_are_rate_limited(client):
    for _ in range(5):
        assert client.post(reverse("login"), {"username": "person@example.com", "password": "wrong"}).status_code == 200
    assert client.post(reverse("login"), {"username": "person@example.com", "password": "wrong"}).status_code == 429
```

- [ ] **Step 2: Implement server-rendered pages**

Build a responsive Django-template interface using locally installed `django-bootstrap5` (no CDN dependency), with a dashboard, persistent navigation bar, task status filter, status badges, task progress as `notified/total`, last successful check, next check, sanitized latest error, compact check/mail history, password change, and the current user's login audit history. The creation screen follows the confirmed sequence: select city, paste filtered Maoyan URL, preview, select visible cinemas and/or enter one manual cinema per line, confirm. Disable task confirmation until SMTP is verified.

```html
<!-- templates/monitoring/task_list.html -->
{% extends "base.html" %}
{% block content %}
<h1>监控任务</h1>
<a href="{% url 'monitoring:task-preview' %}">新建任务</a>
<ul class="task-list">
  {% for task in tasks %}
  <li>
    <a href="{% url 'monitoring:task-detail' task.pk %}">{{ task.movie_name }}</a>
    <span class="status status-{{ task.status|lower }}">{{ task.get_status_display }}</span>
    <span>{{ task.notified_count }}/{{ task.cinema_count }}</span>
    <time datetime="{{ task.next_check_at|date:'c' }}">{{ task.next_check_at }}</time>
  </li>
  {% empty %}<li>尚无监控任务</li>{% endfor %}
</ul>
{% endblock %}
```

Implement `enforce_rate_limit()` with a cache key containing a SHA-256 digest of `scope`, the Nginx-overwritten `HTTP_X_REAL_IP`, and normalized identity. `cache.add(key, 1, timeout=window_seconds)` initializes the window; `cache.incr(key)` increments it; counts above the limit raise a dedicated exception converted to HTTP 429. Apply it to failed login attempts and invalid/expired invitation activations, and clear the login key after successful authentication.

```python
def enforce_rate_limit(request, scope, identity, limit=5, window_seconds=900):
    client_ip = request.META.get("HTTP_X_REAL_IP", request.META.get("REMOTE_ADDR", "unknown"))
    digest = sha256(f"{scope}:{client_ip}:{identity.casefold()}".encode("utf-8")).hexdigest()
    key = f"rate-limit:{digest}"
    if cache.add(key, 1, timeout=window_seconds):
        return
    if cache.incr(key) > limit:
        raise RateLimitExceeded
```

- [ ] **Step 3: Implement safe credential editing**

Render the authorization-code field empty on every GET. An empty code on edit preserves the existing ciphertext; a non-empty value replaces it and resets verification until a test succeeds. Show only host, port, security, username, sender address, verification time, and sanitized latest error.

```python
class SMTPConfigForm(forms.ModelForm):
    authorization_code = forms.CharField(required=False, widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = SMTPConfig
        fields = ("host", "port", "security", "username", "from_email")

    def save(self, commit=True):
        instance = super().save(commit=False)
        secret = self.cleaned_data.get("authorization_code")
        if secret:
            instance.set_password(secret)
            instance.is_verified = False
            instance.verified_at = None
        if commit:
            instance.save()
        return instance
```

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest accounts/tests mailsettings/tests monitoring/tests/test_web.py -v
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
git add templates static accounts mailsettings monitoring ticketwatch
git commit -m "feat: add invite-only monitoring web interface"
```

Expected: page and permission tests pass; production check reports only settings intentionally supplied by deployment environment.

### Task 13: Add admin visibility, auditing, and parser safety controls

**Files:**
- Create: `monitoring/admin.py`
- Create: `accounts/admin.py`
- Create: `mailsettings/admin.py`
- Modify: `monitoring/models.py`
- Modify: `monitoring/tasks.py`
- Modify: `audit/services.py`
- Create: `audit/tests/test_audit.py`
- Create: `monitoring/tests/test_admin_health.py`
- Modify: `ticketwatch/settings.py`

**Interfaces:**
- Consumes: `record_event()`, adapter error categories, monitoring models, Redis queue metrics, and Django staff authorization.
- Produces: safe ModelAdmin registrations; singleton `SystemPolicy`; recursively sanitized audit metadata; `hash_query_key(value: str) -> str`; `record_platform_failure(query_key: str, error_code: str) -> int`; `record_structural_failure(query_key: str, error_code: str) -> int`; `reset_failure_counters(query_key: str) -> None`; Celery task `prune_check_runs() -> tuple[int, int]`; and a staff-only system status summary.

- [ ] **Step 1: Write failing audit and safety tests**

Assert audit records exist for invitation, login, user disable/password reset, SMTP update/test, task create/pause/resume/cancel, admin global/platform/query pause, and polling/retention-policy changes. Assert audit metadata contains none of the authorization code, encrypted credential, Cookie, session key, full invitation token, or email body. Prove the admin can change the three polling bands, values below 60 seconds are rejected by both form and model validation, and the next scheduler decision uses the saved values. Simulate five consecutive parser-structure failures and assert affected tasks enter `ERROR` with a visible system alert, never marked open. Freeze time and prove successful `CheckRun` rows older than 30 days and error rows older than 90 days are pruned while newer rows remain.

```python
@pytest.mark.django_db
def test_audit_metadata_recursively_redacts_secrets(user):
    event = record_event(
        user,
        "smtp.updated",
        metadata={"authorization": "secret", "nested": {"cookie": "session", "safe": 2}},
    )
    assert event.metadata == {"nested": {"safe": 2}}


@pytest.mark.django_db
def test_five_structure_failures_pause_query_group(task_factory):
    task = task_factory(query_key="maoyan:10:query")
    for _ in range(5):
        record_structural_failure(task.query_key, "missing-cinema-list")
    task.refresh_from_db()
    assert task.status == MonitorTask.Status.ERROR
    assert AuditEvent.objects.filter(action="monitor.query.auto_paused").exists()
```

- [ ] **Step 2: Implement minimal safe admin pages**

Register users, invitations, tasks, cinemas, checks, notifications, audit events, and a singleton `SystemPolicy` with read-only timestamps and credential fields excluded. The policy stores global pause, Maoyan pause, the three editable interval bands (defaults 60/300/900 seconds, each validated `>= 60`), structure-failure threshold (default 5), success retention days (default 30), and error retention days (default 90). Add audited admin actions to disable users, initiate Django password resets, pause/resume selected tasks, pause all monitoring, and pause/resume Maoyan; never add an action that sends mail or starts arbitrary URLs. `due_query_groups()` returns no work when global or Maoyan pause is active, and `check_query_group()` converts the current policy row to `PollingIntervals` before persisting each next check. Require staff status and normal Django authentication; use `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`, `SECURE_PROXY_SSL_HEADER`, `X_FRAME_OPTIONS="DENY"`, and no public admin link in ordinary navigation.

```python
@admin.action(description="Pause selected monitoring tasks")
def pause_tasks(modeladmin, request, queryset):
    changed = queryset.exclude(status__in=[MonitorTask.Status.COMPLETED, MonitorTask.Status.EXPIRED, MonitorTask.Status.CANCELLED]).update(
        status=MonitorTask.Status.PAUSED,
        updated_at=timezone.now(),
    )
    record_event(request.user, "monitor.admin.paused", metadata={"count": changed})


@admin.register(MonitorTask)
class MonitorTaskAdmin(admin.ModelAdmin):
    list_display = ("movie_name", "city_name", "show_date", "status", "user", "next_check_at")
    readonly_fields = ("user", "query_key", "created_at", "updated_at", "completed_at", "expired_at")
    actions = (pause_tasks,)
```

```python
class SystemPolicy(models.Model):
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    global_paused = models.BooleanField(default=False)
    maoyan_paused = models.BooleanField(default=False)
    near_interval_seconds = models.PositiveIntegerField(default=60, validators=[MinValueValidator(60)])
    medium_interval_seconds = models.PositiveIntegerField(default=300, validators=[MinValueValidator(60)])
    far_interval_seconds = models.PositiveIntegerField(default=900, validators=[MinValueValidator(60)])
    structure_failure_limit = models.PositiveSmallIntegerField(default=5, validators=[MinValueValidator(1)])
    success_retention_days = models.PositiveSmallIntegerField(default=30, validators=[MinValueValidator(1)])
    error_retention_days = models.PositiveSmallIntegerField(default=90, validators=[MinValueValidator(1)])

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    def polling_intervals(self):
        return PollingIntervals(
            near_seconds=self.near_interval_seconds,
            medium_seconds=self.medium_interval_seconds,
            far_seconds=self.far_interval_seconds,
        )
```

```python
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
```

- [ ] **Step 3: Implement parser circuit breaker and status summary**

After five consecutive `PageStructureError` results for a query key, pause its active tasks, record a high-severity audit event, and expose the affected query count on a staff-only status page. Captcha/rate-limit failures back off but do not count as structural failures. A successful valid parse resets the structural counter.

```python
STRUCTURE_FAILURE_LIMIT = 5


def hash_query_key(value):
    return sha256(value.encode("utf-8")).hexdigest()


def record_platform_failure(query_key, error_code):
    with transaction.atomic():
        tasks = MonitorTask.objects.select_for_update().filter(
            query_key=query_key,
            status__in=[MonitorTask.Status.MONITORING, MonitorTask.Status.PARTIAL],
        )
        tasks.update(
            consecutive_fetch_failures=F("consecutive_fetch_failures") + 1,
            last_error_code=error_code,
            updated_at=timezone.now(),
        )
        maximum = tasks.aggregate(value=Max("consecutive_fetch_failures"))["value"] or 0
        if maximum >= SystemPolicy.objects.get(singleton_key=1).structure_failure_limit:
            changed = tasks.update(status=MonitorTask.Status.ERROR, updated_at=timezone.now())
            record_event(None, "monitor.query.auto_paused", metadata={"query_key_hash": hash_query_key(query_key), "task_count": changed})
        return maximum


def record_structural_failure(query_key, error_code):
    with transaction.atomic():
        tasks = MonitorTask.objects.select_for_update().filter(query_key=query_key)
        active = tasks.filter(status__in=[MonitorTask.Status.MONITORING, MonitorTask.Status.PARTIAL])
        active.update(
            consecutive_structure_failures=F("consecutive_structure_failures") + 1,
            last_error_code=error_code,
            updated_at=timezone.now(),
        )
        maximum = tasks.aggregate(value=Max("consecutive_structure_failures"))["value"] or 0
        if maximum >= SystemPolicy.objects.get(singleton_key=1).structure_failure_limit:
            paused = active.update(status=MonitorTask.Status.ERROR, updated_at=timezone.now())
            record_event(None, "monitor.query.auto_paused", metadata={"query_key_hash": hash_query_key(query_key), "task_count": paused})
        return maximum


def reset_failure_counters(query_key):
    MonitorTask.objects.filter(query_key=query_key).update(
        consecutive_fetch_failures=0,
        consecutive_structure_failures=0,
        last_error_code="",
    )


@shared_task(queue="checks")
def prune_check_runs():
    policy = SystemPolicy.objects.get(singleton_key=1)
    now = timezone.now()
    success_deleted, _ = CheckRun.objects.filter(
        status=CheckRun.Status.SUCCEEDED,
        finished_at__lt=now - timedelta(days=policy.success_retention_days),
    ).delete()
    error_deleted, _ = CheckRun.objects.exclude(status=CheckRun.Status.SUCCEEDED).filter(
        finished_at__lt=now - timedelta(days=policy.error_retention_days),
    ).delete()
    return success_deleted, error_deleted
```

Add `prune_check_runs` to `CELERY_TASK_ROUTES` on `checks` and to `CELERY_BEAT_SCHEDULE` daily at 03:30 Asia/Shanghai. The staff status page reports global/platform pause state, Redis `checks`/`mail` queue depths, due-task lag, last successful check, 24-hour platform/structure error counts, SMTP failure count, and newest backup/certificate health supplied by the deployment verifier.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest audit/tests monitoring/tests/test_admin_health.py -v
.venv/bin/python manage.py check --deploy
.venv/bin/ruff check accounts mailsettings monitoring audit
git add accounts mailsettings monitoring audit ticketwatch
git commit -m "feat: add auditing and monitoring safety controls"
```

Expected: audit redaction, circuit-breaker, and staff authorization tests pass.

## Phase 5: Containers, Alibaba Cloud deployment, and operations

### Task 14: Containerize the application with PostgreSQL and Redis internal-only

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Create: `deploy/entrypoint.sh`
- Modify: `.env.example`
- Create: `tests/test_compose_config.py`

**Interfaces:**
- Consumes: the Django/Celery application, environment contract, migrations, and static assets from Tasks 2–13.
- Produces: one immutable application image and Compose services `nginx`, `web`, `worker-check`, `worker-mail`, `beat`, `postgres`, and `redis` with only Nginx publishing ports.

- [ ] **Step 1: Write a failing Compose policy test**

Parse `docker compose config --format json` and assert the services are `nginx`, `web`, `worker-check`, `worker-mail`, `beat`, `postgres`, and `redis`; only Nginx publishes host ports; Postgres and Redis use health checks and named volumes; all application services use the same immutable image; and workers have distinct queues.

```python
import json
import subprocess


def test_compose_exposes_only_nginx():
    raw = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    services = json.loads(raw)["services"]
    assert set(services) == {"nginx", "web", "worker-check", "worker-mail", "beat", "postgres", "redis"}
    assert services["nginx"]["ports"]
    assert all("ports" not in services[name] for name in services if name != "nginx")
    assert services["postgres"]["healthcheck"]
    assert services["redis"]["healthcheck"]
    assert "-Q checks" in " ".join(services["worker-check"]["command"])
    assert "-Q mail" in " ".join(services["worker-mail"]["command"])
```

- [ ] **Step 2: Build the production image**

Use a multi-stage Python 3.12 slim image, locked dependencies, a non-root `app` user, and Gunicorn. The web entrypoint collects static assets into the shared static volume after migrations. `deploy/entrypoint.sh` waits for PostgreSQL, runs `migrate --noinput` only in the web service, and execs its command. Do not bake `.env`, encryption keys, SMTP data, certificates, or backups into the image.

```dockerfile
FROM python:3.12-slim AS builder
WORKDIR /build
COPY . /build
RUN python -m venv /venv && /venv/bin/pip install --no-cache-dir --upgrade pip && /venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system app && useradd --system --gid app --home /app app
COPY --from=builder /venv /venv
WORKDIR /app
COPY --chown=app:app . /app
RUN mkdir -p /app/staticfiles && chown app:app /app/staticfiles
USER app
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["gunicorn", "ticketwatch.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "30"]
```

Create `.dockerignore` so the build context cannot contain runtime secrets or operator data:

```dockerignore
.git
.venv
__pycache__
*.py[cod]
.pytest_cache
.ruff_cache
.env
.env.*
!.env.example
backups/
*.dump
*.dump.age
certbot/
docs/superpowers/
```

```bash
#!/usr/bin/env bash
set -euo pipefail
python - <<'PY'
import time
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ticketwatch.settings")
django.setup()
from django.db import connection
for attempt in range(30):
    try:
        connection.ensure_connection()
        connection.close()
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(2)
PY
if [[ "${SERVICE_ROLE:-}" == "web" ]]; then
  python manage.py migrate --noinput
  python manage.py collectstatic --noinput
fi
exec "$@"
```

- [ ] **Step 3: Define the Compose topology**

Bind Nginx to `80:80` and `443:443`. Use `expose` rather than `ports` for web/PostgreSQL/Redis. Add health checks, `restart: unless-stopped`, named volumes for PostgreSQL, static files, and certificate webroot, plus log-size rotation. Set worker commands to `celery -A ticketwatch worker -Q checks` and `celery -A ticketwatch worker -Q mail`; beat runs as a singleton service.

```yaml
name: ticketwatch
x-app: &app
  image: ticketwatch:0.1.0
  build: .
  env_file: .env
  restart: unless-stopped
  depends_on:
    postgres: {condition: service_healthy}
    redis: {condition: service_healthy}
  logging:
    options: {max-size: "10m", max-file: "5"}

services:
  nginx:
    image: nginx:1.27-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    depends_on: {web: {condition: service_healthy}}
    volumes:
      - ./deploy/nginx/active.conf:/etc/nginx/conf.d/default.conf:ro
      - staticfiles:/srv/static:ro
      - /var/lib/ticketwatch/certbot-webroot:/var/www/certbot:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
  web:
    <<: *app
    environment: {SERVICE_ROLE: web}
    expose: ["8000"]
    volumes: ["staticfiles:/app/staticfiles"]
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
      interval: 15s
      timeout: 5s
      retries: 5
  worker-check:
    <<: *app
    command: ["celery", "-A", "ticketwatch", "worker", "-Q", "checks", "--loglevel=INFO"]
  worker-mail:
    <<: *app
    command: ["celery", "-A", "ticketwatch", "worker", "-Q", "mail", "--loglevel=INFO"]
  beat:
    <<: *app
    command: ["celery", "-A", "ticketwatch", "beat", "--loglevel=INFO"]
  postgres:
    image: postgres:17-alpine
    env_file: .env
    expose: ["5432"]
    volumes: ["postgres_data:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 10s
      timeout: 5s
      retries: 5
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "no"]
    expose: ["6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
  staticfiles:
```

- [ ] **Step 4: Verify and commit**

```bash
docker compose config --quiet
.venv/bin/pytest tests/test_compose_config.py -v
docker compose build
docker compose up -d postgres redis web worker-check worker-mail beat
docker compose ps
docker compose exec -T web python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status)"
docker compose down
git add Dockerfile docker-compose.yml deploy tests .env.example
git commit -m "build: containerize monitoring services"
```

Expected: policy test passes, all started services become healthy, and health endpoint returns HTTP 200.

### Task 15: Configure Nginx and automated HTTPS for the public IP

**Files:**
- Create: `deploy/nginx/bootstrap.conf`
- Create: `deploy/nginx/https.conf`
- Create: `deploy/nginx/active.conf`
- Create: `scripts/install_ip_certificate.sh`
- Create: `scripts/renew_ip_certificate.sh`
- Create: `docs/runbooks/deploy.md`

**Interfaces:**
- Consumes: internal `web:8000`, shared ACME webroot, Docker Compose project, public IP `47.116.69.108`, and root-only Certbot environment.
- Produces: HTTP bootstrap, HTTPS reverse proxy, validated public-IP certificate installation, 12-hour renewal timer, and safe Nginx deploy hook.

- [ ] **Step 1: Add and test the HTTP bootstrap configuration**

Serve `/.well-known/acme-challenge/` from the shared webroot and proxy other traffic to Django during certificate bootstrap. Set trusted proxy headers, request-size limit, timeouts, security headers, and no direct access to dotfiles. Validate with `nginx -t` inside the container.

Create `bootstrap.conf` and initially copy the same content to `active.conf`:

```nginx
upstream ticketwatch_web { server web:8000; }

server {
    listen 80 default_server;
    server_name 47.116.69.108;
    client_max_body_size 1m;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type text/plain;
        try_files $uri =404;
    }
    location ~ /\. { deny all; }
    location /static/ { alias /srv/static/; access_log off; expires 7d; }
    location / {
        proxy_pass http://ticketwatch_web;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
    }
}
```

Change the Compose Nginx mount to `./deploy/nginx/active.conf:/etc/nginx/conf.d/default.conf:ro`, then run:

```bash
mkdir -p /var/lib/ticketwatch/certbot-webroot/.well-known/acme-challenge
docker compose up -d nginx
docker compose exec -T nginx nginx -t
```

- [ ] **Step 2: Implement repeatable IP-certificate issuance**

`scripts/install_ip_certificate.sh` checks Certbot is at least 5.4, then requests the supported short-lived IP certificate for `47.116.69.108` using the webroot authenticator and non-interactive email/terms parameters supplied by root-only environment file `/etc/ticketwatch/certbot.env`. It validates the resulting certificate contains IP SAN `47.116.69.108` before installing `https.conf` and reloading Nginx. The script exits without changing active Nginx configuration when issuance or validation fails.

Create `https.conf`:

```nginx
upstream ticketwatch_web { server web:8000; }

server {
    listen 80 default_server;
    server_name 47.116.69.108;
    location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; try_files $uri =404; }
    location / { return 301 https://47.116.69.108$request_uri; }
}

server {
    listen 443 ssl;
    server_name 47.116.69.108;
    ssl_certificate /etc/letsencrypt/live/47.116.69.108/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/47.116.69.108/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    client_max_body_size 1m;
    location ~ /\. { deny all; }
    location /static/ { alias /srv/static/; access_log off; expires 7d; }
    location / {
        proxy_pass http://ticketwatch_web;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_connect_timeout 5s;
        proxy_read_timeout 30s;
    }
}
```

Create `scripts/install_ip_certificate.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
public_ip=47.116.69.108
project_dir=/opt/ticketwatch
webroot=/var/lib/ticketwatch/certbot-webroot
env_file=/etc/ticketwatch/certbot.env
test "$(id -u)" -eq 0
test -r "$env_file"
set -a
source "$env_file"
set +a
test -n "${CERTBOT_EMAIL:-}"
installed_version=$(certbot --version | awk '{print $2}')
test "$(printf '%s\n' 5.4.0 "$installed_version" | sort -V | head -n1)" = 5.4.0
staging=()
if [[ "${CERTBOT_STAGING:-false}" == "true" ]]; then staging=(--staging); fi
certbot certonly "${staging[@]}" \
  --non-interactive --agree-tos --email "$CERTBOT_EMAIL" \
  --preferred-profile shortlived \
  --webroot --webroot-path "$webroot" \
  --ip-address "$public_ip" --cert-name "$public_ip"
openssl x509 -in "/etc/letsencrypt/live/$public_ip/cert.pem" -noout -ext subjectAltName \
  | grep -F "IP Address:$public_ip"
cd "$project_dir"
cp deploy/nginx/active.conf deploy/nginx/active.conf.previous
cp deploy/nginx/https.conf deploy/nginx/active.conf
if ! docker compose exec -T nginx nginx -t; then
  cp deploy/nginx/active.conf.previous deploy/nginx/active.conf
  exit 1
fi
docker compose exec -T nginx nginx -s reload
```

- [ ] **Step 3: Automate renewal for six-day certificates**

`scripts/renew_ip_certificate.sh` runs `certbot renew --quiet` and uses a deploy hook that performs `docker compose exec -T nginx nginx -t` before reload. Install a systemd timer that runs every 12 hours with randomized delay. The deploy runbook includes the exact timer unit, log locations, forced dry-run procedure, rollback to HTTP bootstrap, and alert threshold when less than 48 hours remain.

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/ticketwatch
certbot renew --quiet --deploy-hook \
  'cd /opt/ticketwatch && docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload'
```

```ini
# /etc/systemd/system/ticketwatch-cert-renew.service
[Unit]
Description=Renew TicketWatch public IP certificate
After=docker.service network-online.target

[Service]
Type=oneshot
ExecStart=/opt/ticketwatch/scripts/renew_ip_certificate.sh
```

```ini
# /etc/systemd/system/ticketwatch-cert-renew.timer
[Unit]
Description=Run TicketWatch certificate renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Verify on the Alibaba Cloud host**

After the user provides SSH access during execution, run from the server directory:

```bash
docker compose up -d
sudo ./scripts/install_ip_certificate.sh
curl --fail http://47.116.69.108/healthz
curl --fail https://47.116.69.108/healthz
openssl s_client -connect 47.116.69.108:443 -verify_return_error </dev/null
sudo certbot renew --dry-run
```

Expected: HTTP redirects to HTTPS after bootstrap, HTTPS returns 200 without verification error, SAN contains the public IP, and renewal rehearsal succeeds. Alibaba Cloud security-group inbound rules expose only TCP 80/443 for the application; ports 5432, 6379, and 8000 are not publicly reachable.

- [ ] **Step 5: Commit**

```bash
git add deploy/nginx scripts/install_ip_certificate.sh scripts/renew_ip_certificate.sh docs/runbooks/deploy.md
git commit -m "ops: configure public ip https"
```

### Task 16: Add encrypted backups, recovery, and end-to-end acceptance

**Files:**
- Create: `scripts/backup_postgres.sh`
- Create: `scripts/restore_postgres.sh`
- Create: `scripts/verify_deployment.sh`
- Create: `docs/runbooks/backup-restore.md`
- Create: `docs/runbooks/incident-response.md`
- Create: `tests/test_acceptance.py`

**Interfaces:**
- Consumes: the deployed Compose stack, PostgreSQL service, age recipient/identity, HTTPS endpoint, Celery inspection, and operational logs.
- Produces: encrypted backup artifacts, guarded restore rehearsal, deployment verification exit status, incident runbook, and timestamped acceptance evidence.

- [ ] **Step 1: Implement encrypted PostgreSQL backups**

Stream `pg_dump --format=custom` from the Postgres container directly into `age` encryption using a recipient stored in `/etc/ticketwatch/backup.env`; write to a restrictive host directory with a timestamped temporary filename, verify the encrypted file is non-empty, then atomically rename it. Keep seven daily and four weekly backups. Never write an unencrypted dump to disk.

```bash
#!/usr/bin/env bash
set -euo pipefail
umask 077
set -a
source /etc/ticketwatch/backup.env
set +a
test -n "${BACKUP_AGE_RECIPIENT:-}"
test -n "${POSTGRES_USER:-}"
test -n "${POSTGRES_DB:-}"
backup_root=/var/backups/ticketwatch
daily_dir="$backup_root/daily"
weekly_dir="$backup_root/weekly"
mkdir -p "$daily_dir" "$weekly_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$daily_dir/.ticketwatch-$stamp.dump.age.partial"
final="$daily_dir/ticketwatch-$stamp.dump.age"
cd /opt/ticketwatch
docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom \
  | age --recipient "$BACKUP_AGE_RECIPIENT" --output "$temporary"
test -s "$temporary"
mv "$temporary" "$final"
if [[ "$(date -u +%u)" == "7" ]]; then cp -p "$final" "$weekly_dir/${final##*/}"; fi
mapfile -t daily < <(find "$daily_dir" -maxdepth 1 -type f -name 'ticketwatch-*.dump.age' -print | sort -r)
mapfile -t weekly < <(find "$weekly_dir" -maxdepth 1 -type f -name 'ticketwatch-*.dump.age' -print | sort -r)
if ((${#daily[@]} > 7)); then rm -- "${daily[@]:7}"; fi
if ((${#weekly[@]} > 4)); then rm -- "${weekly[@]:4}"; fi
printf '%s\n' "$final"
```

- [ ] **Step 2: Implement guarded restore rehearsal**

`restore_postgres.sh` requires an explicit backup path and `--target-db ticketwatch_restore_test`; it refuses the production database name. Decrypt into `pg_restore` through a pipe, run Django migrations/checks against the restored database, and compare row counts for users, tasks, cinemas, notifications, and audit events. Document quarterly rehearsal and recovery-time evidence.

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/ticketwatch/backup.env
set +a
if [[ $# -ne 3 || "$1" != "--target-db" ]]; then
  printf 'usage: %s --target-db ticketwatch_restore_test BACKUP.age\n' "$0" >&2
  exit 64
fi
target_db=$2
backup=$3
test "$target_db" = ticketwatch_restore_test
test "$target_db" != "$POSTGRES_DB"
test -r "$backup"
test -r "$BACKUP_AGE_IDENTITY"
cd /opt/ticketwatch
docker compose exec -T postgres dropdb -U "$POSTGRES_USER" --if-exists "$target_db"
docker compose exec -T postgres createdb -U "$POSTGRES_USER" "$target_db"
age --decrypt --identity "$BACKUP_AGE_IDENTITY" "$backup" \
  | docker compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$target_db" --no-owner --exit-on-error
export target_db
restore_url=$(python3 -c 'import os,urllib.parse; q=lambda value: urllib.parse.quote(value,safe=""); print("postgresql://{}:{}@postgres:5432/{}".format(q(os.environ["POSTGRES_USER"]),q(os.environ["POSTGRES_PASSWORD"]),os.environ["target_db"]))')
docker compose run --rm -e DATABASE_URL="$restore_url" -e SERVICE_ROLE=restore web python manage.py migrate --noinput
docker compose run --rm -e DATABASE_URL="$restore_url" -e SERVICE_ROLE=restore web python manage.py check
for table in accounts_user monitoring_monitortask monitoring_taskcinema monitoring_notification audit_auditevent; do
  production_count=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select count(*) from $table")
  restored_count=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$target_db" -Atc "select count(*) from $table")
  test "$production_count" = "$restored_count"
done
```

- [ ] **Step 3: Add deployment health verification**

`verify_deployment.sh` checks HTTPS and certificate lifetime, Compose health, Celery worker/beat availability, queue depth, last successful check, parser error rate, SMTP failure count, most recent encrypted backup, and that 5432/6379/8000 are not bound on the public interface. It prints no secret environment values.

```bash
#!/usr/bin/env bash
set -euo pipefail
public_ip=47.116.69.108
cd /opt/ticketwatch
curl --fail --silent --show-error "https://$public_ip/healthz" >/dev/null
openssl s_client -connect "$public_ip:443" -verify_return_error </dev/null 2>/dev/null \
  | openssl x509 -checkend 172800 -noout
docker compose ps --format json | python3 -c 'import json,sys; rows=json.load(sys.stdin); assert rows and all(r.get("State") == "running" for r in rows)'
docker compose exec -T worker-check celery -A ticketwatch inspect ping --timeout 10 >/dev/null
docker compose exec -T worker-mail celery -A ticketwatch inspect ping --timeout 10 >/dev/null
docker compose exec -T redis redis-cli LLEN checks
docker compose exec -T redis redis-cli LLEN mail
docker compose exec -T web python manage.py shell -c \
  'from monitoring.models import CheckRun,Notification; from django.utils import timezone; from datetime import timedelta; assert CheckRun.objects.filter(status="SUCCEEDED",finished_at__gte=timezone.now()-timedelta(hours=1)).exists(); print(Notification.objects.filter(status__in=["FAILED","PERMANENT_FAILED"]).count())'
test -n "$(find /var/backups/ticketwatch/daily -type f -name '*.dump.age' -mtime -2 -print -quit)"
for port in 5432 6379 8000; do
  if ss -lnt | awk '{print $4}' | grep -Eq "(^|:)$port$"; then
    printf 'internal port %s is bound on the host\n' "$port" >&2
    exit 1
  fi
done
```

- [ ] **Step 4: Run the full automated suite**

```bash
.venv/bin/pytest -v
.venv/bin/ruff check .
.venv/bin/python manage.py check --deploy
docker compose config --quiet
docker compose build
```

Expected: all tests pass, Ruff is clean, Django deployment checks pass with production environment variables, and the image builds.

- [ ] **Step 5: Run the production acceptance checklist**

On `47.116.69.108`, prove the confirmed design's 14 acceptance criteria with timestamps and redacted logs:

1. Admin invitation succeeds and `/accounts/register/` remains unavailable.
2. Cross-user list/detail/action attempts cannot read or mutate another user's data.
3. Per-user SMTP saves encrypted authorization data, never echoes it, and delivers a test mail.
4. City + Maoyan URL + selected/manual single- and multi-cinema task creation succeeds.
5. Fixed sanitized fixtures distinguish open, valid-closed, captcha/rate-limited, and malformed pages.
6. One opening notification per cinema is enforced; temporary retry recovers while the detected fact remains.
7. A task whose target is already open triggers the immediate initial check and mail path.
8. A multi-cinema task moves through partial state and completes only after every mail succeeds.
9. The Asia/Shanghai end boundary stops unfinished polling and sends one expiry mail.
10. Admin-edited polling bands take effect, values below 60 seconds are rejected, effective platform checks remain at least 60 seconds apart, and identical query keys fetch once.
11. Restarting web, workers, beat, and Redis preserves PostgreSQL state and does not replay a sent notification.
12. HTTPS is trusted, HTTP redirects, and PostgreSQL/Redis/Gunicorn ports are not publicly reachable.
13. Certificate renewal and a fresh encrypted backup restore into `ticketwatch_restore_test` both pass rehearsal.
14. Five consecutive structural failures cannot create an opening; they enter `ERROR`, appear in staff status, and can be explicitly resumed after repair.

Record the result in `acceptance-YYYYMMDD.json` using this exact schema:

```json
{
  "public_ip": "47.116.69.108",
  "checked_at": "2026-08-16T00:00:00+08:00",
  "checks": {
    "invite_only": "pass",
    "owner_isolation": "pass",
    "smtp_test": "pass",
    "task_creation": "pass",
    "page_classification": "pass",
    "per_cinema_idempotency": "pass",
    "immediate_open": "pass",
    "multi_cinema_completion": "pass",
    "expiry": "pass",
    "polling_floor_and_grouping": "pass",
    "restart_recovery": "pass",
    "trusted_https_and_private_ports": "pass",
    "certificate_and_backup_rehearsal": "pass",
    "parser_circuit_breaker": "pass"
  },
  "evidence_directory": "acceptance-evidence/20260816"
}
```

- [ ] **Step 6: Commit the operational baseline**

```bash
git add scripts docs/runbooks tests
git commit -m "ops: add backup recovery and acceptance checks"
git status --short
```

Expected: the commit succeeds and `git status --short` is empty.

## Completion gate

Do not declare the first release complete until every acceptance item in the confirmed design is backed by a passing automated test or recorded deployment rehearsal. In particular, a successful HTML fetch alone is not proof of availability: only a valid page plus an exact cinema match and a bookable link may create a detection. PostgreSQL is the durable source of truth, Redis loss must be recoverable, notification uniqueness must be enforced in the database, the effective polling interval must remain at least 60 seconds, and the public service must be reachable only through trusted HTTPS.
