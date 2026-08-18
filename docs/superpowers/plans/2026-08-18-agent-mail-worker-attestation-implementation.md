# Agent Mail Worker Verification Attestation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让生产 Web 在不安装 Agent Mail CLI、不挂载 keyring 的前提下，基于 Worker 写入 PostgreSQL 的新鲜验证证明创建监控任务，并从页面异步请求测试邮件。

**Architecture:** Worker 是唯一 Agent Mail 凭据消费者。`AgentMailConfig` 保存异步验证请求、数据库租约和有时效的验证证明；Web 只读取证明、固定地址和 Worker 心跳。Redis 仅用于加速唤醒，PostgreSQL 是请求与结果的事实来源。

**Tech Stack:** Python 3.12、Django 5.2、PostgreSQL 16、Redis 7、pytest、Docker Compose、Agent Mail CLI 1.0.15。

**Spec:** `docs/superpowers/specs/2026-08-18-agent-mail-worker-attestation-design.md`

## Global Constraints

- Web 镜像不得安装 `agently-cli`，Web 服务不得挂载 `${AGENTLY_DATA_DIR}`。
- Worker 是唯一可以调用 Agent Mail CLI、Linux keyring 和 OAuth 凭据的长期服务。
- 固定发件人必须是 `mijiatong@agent.qq.com`，固定收件人必须是 `850634546@qq.com`。
- Worker 身份复验间隔默认 `21600` 秒，Web 接受的证明有效期默认 `86400` 秒，请求租约默认 `120` 秒。
- Redis 唤醒失败不得丢失 PostgreSQL 中的验证请求。
- Agent Mail 错误只保存既有去敏错误码，不保存 token、CLI stdout/stderr 或完整邮件正文。
- Worker 邮箱验证失败不得停止电影检查循环。
- 实际发送每封邮件前继续执行实时 `verify_agent_mail()`。
- 所有行为修改先写失败测试，再写最小实现；每项任务完成后独立提交并评审。

---

### Task 1: 验证证明数据模型与纯数据库就绪判定

**Files:**
- Modify: `core/models.py`
- Create: `core/migrations/0008_agent_mail_verification_state.py`
- Modify: `ticketwatch/settings.py`
- Modify: `.env.example`
- Create: `core/services/mail_readiness.py`
- Modify: `core/services/runtime_health.py`
- Create: `core/tests/test_mail_readiness.py`
- Modify: `core/tests/test_migrations.py`
- Modify: `tests/test_production_config.py`

**Interfaces:**
- Produces: `AgentMailConfig.VerificationStatus` with `PENDING`, `VERIFIED`, `FAILED`.
- Produces: `mail_readiness(now: datetime | None = None) -> MailReadiness`.
- Produces: `worker_heartbeat_is_fresh(state: RuntimeState | None, now: datetime) -> bool`.
- Produces settings: `AGENT_MAIL_REVERIFY_SECONDS`, `AGENT_MAIL_ATTESTATION_TTL_SECONDS`, `AGENT_MAIL_CLAIM_SECONDS`.

- [ ] **Step 1: Write failing settings and model tests**

Add production-config assertions:

```python
def test_agent_mail_attestation_defaults(settings):
    assert settings.AGENT_MAIL_REVERIFY_SECONDS == 21600
    assert settings.AGENT_MAIL_ATTESTATION_TTL_SECONDS == 86400
    assert settings.AGENT_MAIL_CLAIM_SECONDS == 120
```

Add model assertions that the singleton defaults to `FAILED`, has no request/claim timestamps, and has no claim token. Add two migration cases; each case starts at migration state `0007` with one singleton row, migrates to `0008`, and asserts a verified row becomes `VERIFIED` while an unverified row becomes `FAILED`.

- [ ] **Step 2: Run the new tests and verify RED**

```bash
.venv/bin/pytest -q tests/test_production_config.py core/tests/test_migrations.py
```

Expected: failures for missing settings, fields, enum, and migration.

- [ ] **Step 3: Add settings, model fields, and migration**

Add to `ticketwatch/settings.py`:

```python
AGENT_MAIL_REVERIFY_SECONDS = int(
    os.environ.get("AGENT_MAIL_REVERIFY_SECONDS", "21600")
)
AGENT_MAIL_ATTESTATION_TTL_SECONDS = int(
    os.environ.get("AGENT_MAIL_ATTESTATION_TTL_SECONDS", "86400")
)
AGENT_MAIL_CLAIM_SECONDS = int(os.environ.get("AGENT_MAIL_CLAIM_SECONDS", "120"))
```

Add the same three exact defaults to `.env.example`.

Add to `AgentMailConfig`:

```python
class VerificationStatus(models.TextChoices):
    PENDING = "PENDING", "等待 Worker 验证"
    VERIFIED = "VERIFIED", "已验证"
    FAILED = "FAILED", "验证失败"

verification_status = models.CharField(
    max_length=16,
    choices=VerificationStatus.choices,
    default=VerificationStatus.FAILED,
)
verification_requested_at = models.DateTimeField(null=True, blank=True)
verification_completed_at = models.DateTimeField(null=True, blank=True)
verification_claim_token = models.UUIDField(null=True, blank=True)
verification_claim_expires_at = models.DateTimeField(null=True, blank=True)
```

Create `0008_agent_mail_verification_state.py` with five `AddField` operations and:

```python
def forwards(apps, schema_editor):
    config_model = apps.get_model("core", "AgentMailConfig")
    config_model.objects.filter(is_verified=True).update(
        verification_status="VERIFIED"
    )
    config_model.objects.filter(is_verified=False).update(
        verification_status="FAILED"
    )


def backwards(apps, schema_editor):
    return None
```

- [ ] **Step 4: Write failing readiness tests**

Create `core/tests/test_mail_readiness.py` with:

```python
@pytest.mark.django_db
def test_mail_readiness_accepts_fresh_attestation_and_worker(settings):
    now = timezone.now()
    config = AgentMailConfig.get_solo()
    config.is_verified = True
    config.verification_status = AgentMailConfig.VerificationStatus.VERIFIED
    config.verified_at = now - timedelta(hours=1)
    config.save()
    RuntimeState.objects.create(worker_heartbeat_at=now - timedelta(seconds=5))

    assert mail_readiness(now=now) == MailReadiness(True, "ready")
```

Add separate tests for `mail-unverified`, `mail-attestation-stale`, `mail-address-mismatch`, and `worker-stale`. Freeze time and assert exactly 24 hours is accepted while 24 hours plus one microsecond is stale.

- [ ] **Step 5: Run readiness tests and verify RED**

```bash
.venv/bin/pytest -q core/tests/test_mail_readiness.py
```

Expected: collection failure because `core.services.mail_readiness` does not exist.

- [ ] **Step 6: Implement the pure readiness service**

Create `core/services/mail_readiness.py`:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from core.models import AgentMailConfig, RuntimeState
from core.services.agent_mail import AGENT_MAIL_SENDER
from core.services.runtime_health import worker_heartbeat_is_fresh

AGENT_MAIL_RECIPIENT = "850634546@qq.com"


@dataclass(frozen=True)
class MailReadiness:
    ready: bool
    code: str


def mail_readiness(now: datetime | None = None) -> MailReadiness:
    now = now or timezone.now()
    config = AgentMailConfig.get_solo()
    if (
        config.sender_email != AGENT_MAIL_SENDER
        or config.recipient_email != AGENT_MAIL_RECIPIENT
    ):
        return MailReadiness(False, "mail-address-mismatch")
    if (
        not config.is_verified
        or config.verification_status
        != AgentMailConfig.VerificationStatus.VERIFIED
        or config.verified_at is None
    ):
        return MailReadiness(False, "mail-unverified")
    ttl = timedelta(seconds=settings.AGENT_MAIL_ATTESTATION_TTL_SECONDS)
    if config.verified_at < now - ttl:
        return MailReadiness(False, "mail-attestation-stale")
    state = RuntimeState.objects.filter(pk=1).first()
    if not worker_heartbeat_is_fresh(state, now):
        return MailReadiness(False, "worker-stale")
    return MailReadiness(True, "ready")
```

Extract `worker_heartbeat_is_fresh` in `runtime_health.py`, preserving threshold `max(WORKER_SCAN_SECONDS * 3, 30)`, and make `_worker_status` consume it.

- [ ] **Step 7: Run Task 1 checks**

```bash
.venv/bin/pytest -q core/tests/test_mail_readiness.py core/tests/test_runtime_health.py core/tests/test_migrations.py tests/test_production_config.py
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/ruff check core/models.py core/services/mail_readiness.py core/services/runtime_health.py ticketwatch/settings.py
```

Expected: all pass, no migration drift, lint exits 0.

- [ ] **Step 8: Commit Task 1**

```bash
git add .env.example core/models.py core/migrations/0008_agent_mail_verification_state.py core/services/mail_readiness.py core/services/runtime_health.py core/tests/test_mail_readiness.py core/tests/test_migrations.py tests/test_production_config.py ticketwatch/settings.py
git commit -m "feat: add worker mail attestation state"
```

---

### Task 2: Worker 异步验证租约与周期复验

**Files:**
- Create: `core/services/mail_verification.py`
- Modify: `core/services/agent_mail.py`
- Modify: `core/services/notifications.py`
- Modify: `core/management/commands/agent_mail_preflight.py`
- Modify: `core/worker.py`
- Create: `core/tests/test_mail_verification.py`
- Modify: `core/tests/test_worker.py`
- Modify: `core/tests/test_agent_mail_preflight.py`

**Interfaces:**
- Consumes: Task 1 model fields and settings.
- Produces: `request_mail_verification(now: datetime | None = None) -> None`.
- Produces: `process_mail_verification(now: datetime | None = None, force_identity: bool = False) -> bool`.
- Produces: `safe_agent_mail_error_code(error: Exception) -> str`.

- [ ] **Step 1: Write failing request and lease tests**

Create transaction-aware tests asserting request sets `PENDING`, writes its timestamp, clears claims, and wakes only on commit; two claimers cannot receive one request; expired claims are reclaimed; old tokens and older requests cannot complete a newer request; Redis notification failure leaves the PostgreSQL request pending.

```python
request_mail_verification(now=now)
config.refresh_from_db()
assert config.verification_status == AgentMailConfig.VerificationStatus.PENDING
assert config.verification_requested_at == now
```

- [ ] **Step 2: Run lease tests and verify RED**

```bash
.venv/bin/pytest -q core/tests/test_mail_verification.py
```

Expected: collection failure because `mail_verification.py` is absent.

- [ ] **Step 3: Implement request, claim, and guarded completion**

Define:

```python
@dataclass(frozen=True)
class MailVerificationClaim:
    token: uuid.UUID
    requested_at: datetime
```

`request_mail_verification()` runs in `transaction.atomic()`, locks the singleton, sets `PENDING`, replaces request time, clears claim fields, and registers `notify_worker` with `transaction.on_commit`.

`_claim_pending_verification()` uses `select_for_update()`, accepts only `PENDING`, rejects a non-expired claim, and stores a new UUID plus `now + AGENT_MAIL_CLAIM_SECONDS`.

`_complete_claim()` must filter by all of:

```python
AgentMailConfig.objects.filter(
    pk=1,
    verification_status=AgentMailConfig.VerificationStatus.PENDING,
    verification_requested_at=claim.requested_at,
    verification_claim_token=claim.token,
)
```

It clears claim fields and writes `verification_completed_at`.

- [ ] **Step 4: Centralize safe error classification**

Move the existing exact error mappings from `agent_mail_preflight.py` into `safe_agent_mail_error_code(error)` in `core/services/agent_mail.py`. Make the management command import it and preserve all existing command error strings.

Update preflight success/failure writes to include `verification_status` and `verification_completed_at`. Update the notification transport's auth/config unverify transition to set `verification_status=FAILED`, clear claim fields, and retain only the safe error code; actual mail transport must not leave the new attestation state claiming `VERIFIED` after credentials fail.

- [ ] **Step 5: Write failing Worker processing tests**

Cover pending test-mail success; safe failure state for identity/auth/config/temporary errors; forced startup verification; successful periodic refresh; no call before six hours; due at exactly six hours; failed checks throttled by `verification_completed_at`; handled errors not escaping Worker; and `send_agent_mail` still performing live identity verification.

- [ ] **Step 6: Implement Worker processing**

`process_mail_verification()` must:

1. claim a pending manual request and call `test_agent_mail_config`;
2. otherwise verify identity when `force_identity=True`;
3. otherwise verify only when `verification_completed_at` is absent or at least `AGENT_MAIL_REVERIFY_SECONDS` old;
4. persist only safe success/failure fields;
5. return `True` only when an attempt occurred.

Periodic completion must filter by the pre-call `updated_at` and non-pending status so it cannot overwrite a concurrent manual request.

- [ ] **Step 7: Integrate Worker order**

Make mail processing the first due-work operation:

```python
mail = process_mail_verification(now=now)
expired = expire_due_task(now=now)
notifications = dispatch_due_notifications(now=now)
claim = claim_due_task(now=now)
```

Return `{"mail": mail, "expired": expired, "notifications": notifications, "checked": checked}`. Call `process_mail_verification(force_identity=True)` once in `run_forever()` before recording startup heartbeat. A handled mail failure must not prevent heartbeat or checks.

- [ ] **Step 8: Run Task 2 tests**

```bash
.venv/bin/pytest -q core/tests/test_mail_verification.py core/tests/test_worker.py core/tests/test_agent_mail.py core/tests/test_agent_mail_preflight.py core/tests/test_agent_mail_notifications.py
.venv/bin/ruff check core/services/mail_verification.py core/services/agent_mail.py core/services/notifications.py core/management/commands/agent_mail_preflight.py core/worker.py
```

- [ ] **Step 9: Commit Task 2**

```bash
git add core/services/mail_verification.py core/services/agent_mail.py core/services/notifications.py core/management/commands/agent_mail_preflight.py core/worker.py core/tests/test_mail_verification.py core/tests/test_worker.py core/tests/test_agent_mail_preflight.py core/tests/test_agent_mail_notifications.py
git commit -m "feat: verify agent mail in worker"
```

---

### Task 3: Web 异步验证界面与任务创建门禁

**Files:**
- Modify: `core/views.py`
- Modify: `core/services/tasks.py`
- Modify: `templates/base.html`
- Modify: `templates/core/mail_form.html`
- Modify: `templates/core/dashboard.html`
- Modify: `core/tests/test_mail_config.py`
- Modify: `core/tests/test_task_creation.py`
- Modify: `tests/test_mvp_flow.py`
- Modify: `core/tests/conftest.py`

**Interfaces:**
- Consumes: `mail_readiness()` and `request_mail_verification()`.
- Removes: all Web-path calls to `verify_agent_mail()` and `test_agent_mail_config()`.
- Produces: page states `PENDING`, `VERIFIED`, `FAILED` with automatic refresh for `PENDING`.

- [ ] **Step 1: Write failing Web isolation tests**

Replace synchronous mail-test expectations with:

```python
@pytest.mark.django_db(transaction=True)
def test_mail_test_only_enqueues_worker_request(client, mocker):
    request_verification = mocker.patch("core.views.request_mail_verification")

    response = client.post(reverse("core:mail-test"))

    assert response.status_code == 302
    assert response.url == reverse("core:mail-edit")
    request_verification.assert_called_once_with()
```

Patch `core.services.agent_mail._run` to raise if called during `mail_test`, `task_preview`, or `task_confirm`; prove these Web paths never execute CLI. Add page tests for pending auto-refresh, verified timestamp, failed safe error, and a distinct Worker-stale notice.

- [ ] **Step 2: Write failing task-creation readiness tests**

Cover:

- fresh attestation plus fresh heartbeat creates a task even when `agently-cli` is absent;
- stale attestation rejects and requests background verification once;
- unverified mail rejects without CLI;
- stale Worker returns a Worker-specific message without clearing mail verification;
- fixed-address mismatch creates zero tasks.

The success fixture creates `RuntimeState(worker_heartbeat_at=timezone.now())`, sets `verification_status=VERIFIED`, and no longer patches `core.services.tasks.verify_agent_mail`.

- [ ] **Step 3: Run Web tests and verify RED**

```bash
.venv/bin/pytest -q core/tests/test_mail_config.py core/tests/test_task_creation.py tests/test_mvp_flow.py
```

Expected: failures because views still call CLI and tasks ignore attestation freshness/heartbeat.

- [ ] **Step 4: Convert mail-test endpoint to enqueue-only**

Remove `AgentMailError` and `test_agent_mail_config` from `core/views.py` and implement:

```python
@require_POST
def mail_test(request):
    request_mail_verification()
    return redirect("core:mail-edit")
```

Delete `_begin_agent_mail_test`, `_mark_agent_mail_verified`, and `_mark_agent_mail_failed`; only Worker services may perform those transitions.

- [ ] **Step 5: Replace live CLI task checks**

In `create_task()`, remove transport imports and map readiness codes exactly:

```python
readiness = mail_readiness()
if not readiness.ready:
    if readiness.code == "mail-attestation-stale":
        request_mail_verification()
        message = "邮箱验证已过期，后台正在重新验证，请稍后重试。"
    elif readiness.code == "worker-stale":
        message = "后台 Worker 暂不可用，请恢复服务后重试。"
    elif readiness.code == "mail-address-mismatch":
        message = "固定邮件地址配置不匹配，请检查服务器配置。"
    else:
        message = "请先验证 Agent Mail 配置。"
    raise TaskCreationError(message)
```

Keep preview's existing database-only `is_verified` prerequisite so a user can prepare a signed preview while Worker health changes; do not call CLI there. Apply the complete freshness/address/heartbeat gate only inside `create_task()` at confirmation. Preserve signed preview, date, cinema validation and transaction behavior.

- [ ] **Step 6: Render asynchronous status**

Update `mail_form.html`:

- add `{% block extra_head %}{% endblock %}` inside `base.html`'s `<head>` and, for `PENDING`, render `<meta http-equiv="refresh" content="2">` from that block;
- `PENDING`: show “等待后台 Worker 验证” and a disabled button;
- `VERIFIED`: show `verified_at` and “Worker 已确认固定邮箱身份”；
- `FAILED`: show only safe error and device-login guidance;
- state that credentials live only in the server Worker keyring, replacing incorrect macOS copy.

Pass `runtime_status=collect_runtime_status()` from `mail_edit` and render Worker stale separately. Update dashboard copy to describe Worker-owned verification.

- [ ] **Step 7: Run Task 3 tests**

```bash
.venv/bin/pytest -q core/tests/test_mail_config.py core/tests/test_task_creation.py tests/test_mvp_flow.py core/tests/test_runtime_health.py
.venv/bin/ruff check core/views.py core/services/tasks.py core/tests/conftest.py
```

- [ ] **Step 8: Commit Task 3**

```bash
git add core/views.py core/services/tasks.py templates/base.html templates/core/mail_form.html templates/core/dashboard.html core/tests/test_mail_config.py core/tests/test_task_creation.py tests/test_mvp_flow.py core/tests/conftest.py
git commit -m "feat: consume worker mail attestation in web"
```

---

### Task 4: 安全契约、文档与完整本地集成

**Files:**
- Modify: `tests/test_compose_config.py`
- Modify: `tests/test_docker_build.py`
- Modify: `docs/server-deployment.md`
- Modify: `tests/test_server_deployment_docs.py`
- Modify only when a failing regression proves a defect in Tasks 1–3.

**Interfaces:**
- Verifies Tasks 1–3 together without real credentials.
- Produces reviewed deployment instructions for migration `0008` and Worker-owned verification.

- [ ] **Step 1: Strengthen image and Compose boundary tests**

Assert:

- Web image returns nonzero for `command -v agently-cli`.
- Web has no `/home/ticketwatch` or `${AGENTLY_DATA_DIR}` mount.
- Worker retains one Agent Mail bind with `create_host_path: false`.
- Compose still contains exactly `web`, `worker`, `postgres`, `redis`.
- Worker remains UID/GID 10001 with 0700 runtime paths.

- [ ] **Step 2: Update runbook and docs tests**

Document migration `core.0008_agent_mail_verification_state`; Worker startup attestation; Web never running CLI; mail button persisting a Worker request; mail failures not stopping movie checks; and final task creation through the production Web/service path. Make docs tests require these statements and reject any Web Agent Mail mount.

- [ ] **Step 3: Run full local quality gates**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check . --exclude monitor.py
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
git diff --check
```

Expected: all tests pass except the existing PostgreSQL-only skip when no PostgreSQL URL is supplied; all other commands exit 0.

- [ ] **Step 4: Run isolated Compose integration**

Use Task 12's verified `/tmp/ticketwatch-integration-data` procedure and non-conflicting loopback port. Use the build-only `test` target, never a fifth production service. Prove Web without CLI creates a task after fresh DB attestation/heartbeat; stale proof queues one request; Redis loss retains it; Worker restart reclaims an expired lease; all four services recover loopback-only.

- [ ] **Step 5: Clean isolated resources and prove scope**

```bash
docker compose --env-file .env.integration ps
docker compose --env-file .env.integration down --volumes
test "$(realpath /tmp/ticketwatch-integration-data)" = "/tmp/ticketwatch-integration-data"
rm -rf -- /tmp/ticketwatch-integration-data
test ! -e .env.integration
```

Verify project containers, networks and volumes are zero; real `db.sqlite3` and `~/.agently-cli` remain.

- [ ] **Step 6: Commit Task 4**

```bash
git add tests/test_compose_config.py tests/test_docker_build.py docs/server-deployment.md tests/test_server_deployment_docs.py
git commit -m "docs: deploy worker mail attestation"
```

If integration proves a defect, add its regression test and minimum fix after RED/GREEN evidence before this commit.

---

### Task 5: 审阅版本发布、服务器升级与真实闭环恢复

**Files:**
- Server state: `/opt/ticketwatch/`
- Evidence: `.superpowers/sdd/2026-08-17-lightweight-private-server-deployment-implementation/task-13-report.md`
- No tracked source changes unless a server-proven defect first receives TDD coverage and fresh review.

**Interfaces:**
- Consumes independently reviewed Tasks 1–4.
- Produces the running private product and completes the original Task 13 sixteen-item acceptance.

- [ ] **Step 1: Publish and verify exact release**

On Mac:

```bash
RELEASE_SHA=$(git rev-parse HEAD)
git push origin feature/ticket-monitor
test "$(git ls-remote origin refs/heads/feature/ticket-monitor | awk '{print $1}')" = "$RELEASE_SHA"
git bundle create /tmp/ticketwatch-release.bundle feature/ticket-monitor
shasum -a 256 /tmp/ticketwatch-release.bundle
```

Transfer with the authorized ED25519 identity, compare SHA-256 on both sides, fetch the bundle, and require server `git rev-parse HEAD` to equal `RELEASE_SHA` before migration.

- [ ] **Step 2: Rebuild and transport linux/amd64 images**

Build Web and Worker from the clean reviewed checkout for `linux/amd64`. Pull platform-specific PostgreSQL 16 Bookworm and Redis 7 Alpine, export all four, gzip, compare local/server SHA-256, load, and verify every server image reports `amd64`. Remove only the verified staging archive.

- [ ] **Step 3: Record upgrade and migrate to 0008**

Persist old/new SHA, image IDs, backup path and hash in `/opt/ticketwatch/data/upgrade-records`. Stop Worker, run protected backup and verifier, then:

```bash
cd /opt/ticketwatch/app
set -Eeuo pipefail
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web python manage.py showmigrations core
```

Require `0008_agent_mail_verification_state` applied before Web/Worker startup.

- [ ] **Step 4: Verify Worker identity and Web isolation**

Start Web/Worker with pulls disabled. Require Web health 200 and no CLI; fresh Worker heartbeat; startup status `VERIFIED`; exact identity after Worker restart; a Web-submitted fixed-recipient test request reaches `VERIFIED` and queues one mail; evidence contains no token, CLI JSON or mail body.

- [ ] **Step 5: Resume real Maoyan acceptance**

Create a current real target through production Web/service path, not direct ORM. Record UUID. Run immediate check and require `CheckRun=SUCCEEDED`, detected/completed terminal state, one opening notification, one queued/sent mail, and no second notification/mail after repeating.

- [ ] **Step 6: Complete remaining server acceptance**

Complete three tasks exercising 60/300/900 seconds and rejecting 59; Web/Worker/Redis/full-stack restart durability; manifest stability across Redis loss; loopback-only 8000 and no 5432/6379 host ports; systemd backup timer, manual backup, isolated restore, off-host copy and SHA; all sixteen sanitized acceptance entries.

- [ ] **Step 7: Run final server gate**

```bash
cd /opt/ticketwatch/app
docker compose --env-file /opt/ticketwatch/.env ps
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/statusz
systemctl status ticketwatch-backup.timer --no-pager
ss -lntp
```

Expected: four TicketWatch services running, Web/dependencies healthy, Worker heartbeat fresh, mail verified, backup present/timer active, Web loopback-only, no PostgreSQL/Redis host listeners.

- [ ] **Step 8: Record completion without secrets**

Update Task 13 report with UUIDs, statuses, counts, timestamps, Git SHA, image IDs, backup SHA-256, port results and safe error codes only. Remove exact-path migration/image/Git staging files. Preserve production data, backups, OAuth keyring, SearXNG/OpenClaw, and local SQLite.
