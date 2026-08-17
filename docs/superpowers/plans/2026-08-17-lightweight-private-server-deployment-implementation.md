# TicketWatch Lightweight Private Server Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有 TicketWatch 本地 MVP 迁移成一套只通过 SSH 隧道访问、以 PostgreSQL 持久化、由单独轻量 Worker 长期调度并使用 Agent Mail 发信的服务器版。

**Architecture:** Django Web 和单进程 Worker 使用同一业务代码，通过 PostgreSQL 保存全部永久状态；Worker 用数据库租约领取到期任务，Redis 只负责唤醒和短期协调。四个服务由 Docker Compose 管理，Web 仅绑定服务器回环地址，Agent Mail 在 Worker 的持久化 Linux 密钥库中单独授权。

**Tech Stack:** Python 3.12、Django 5.2、PostgreSQL 16、Redis 7、Gunicorn、WhiteNoise、Docker 24、Docker Compose plugin、Agent Mail CLI 1.0.15、pytest、ruff。

**Spec:** `docs/superpowers/specs/2026-08-17-lightweight-private-server-deployment-design.md`

## Global Constraints

- 目标服务器固定为 Alibaba Cloud Linux 4、2 CPU、1.8 GiB RAM、6 GiB Swap、40 GB 系统盘。
- 第一阶段保持单用户，不增加登录、注册、多用户、OpenClaw、LLM、SMTP、Nginx、Certbot 或公网 Web。
- Web 只能发布到宿主机 `127.0.0.1:8000`；安全组只开放 `22/TCP`。
- PostgreSQL 是任务、检查、通知和设置的唯一永久数据源；Redis 丢失不得造成业务数据丢失。
- Worker 只有一个进程、并发为 1；不引入 Celery 或 Celery Beat。
- 检查间隔分为默认 60/300/900 秒三档，所有可配置间隔下限均为 60 秒。
- 时区固定为 `Asia/Shanghai`；目标观影日当天仍按紧急间隔监控，次日才过期。
- Agent Mail 发件人固定为 `mijiatong@agent.qq.com`，收件人固定为 `850634546@qq.com`，服务器 workspace 固定为 `ticketwatch-server`。
- 任何日志、测试输出和备份不得包含 OAuth token、数据库密码、完整 Cookie 或原始敏感响应。
- 所有功能变更遵循 TDD：先看到目标测试失败，再写最小实现，再运行局部和完整测试。
- 迁移和部署不得删除现有 `db.sqlite3`；PostgreSQL 恢复演练通过前始终保留 SQLite 备份。

## File Structure

### 新建文件

- `ticketwatch/config.py`：纯函数解析运行环境、数据库和安全设置。
- `core/services/scheduling.py`：三级检查区间和下一次检查时间计算。
- `core/services/leases.py`：PostgreSQL 任务租约领取与校验。
- `core/services/coordination.py`：Redis 唤醒发送与阻塞等待。
- `core/worker.py`：一次后台工作和常驻 Worker 主循环。
- `core/services/runtime_health.py`：运行状态聚合，不返回敏感信息。
- `core/services/data_manifest.py`：迁移前后数据清单和摘要。
- `core/management/commands/runworker.py`：独立 Worker 进程入口。
- `core/management/commands/agent_mail_preflight.py`：服务器 Agent Mail 身份及真实测试邮件预检。
- `core/management/commands/data_manifest.py`：输出迁移校验清单。
- `core/management/commands/mark_backup_success.py`：备份成功后写入运行状态。
- `core/migrations/0005_server_runtime_fields.py`：三级设置、任务租约、通知枚举和固定邮件配置。
- `core/migrations/0006_runtime_state.py`：Worker 和备份心跳模型。
- `tests/test_production_config.py`：生产配置和安全失败测试。
- `core/tests/test_scheduling.py`：三级频率边界测试。
- `core/tests/test_leases.py`：任务租约和过期回收测试。
- `core/tests/test_worker.py`：Worker 顺序、Redis 降级和停止测试。
- `core/tests/test_runtime_health.py`：状态聚合与去敏输出测试。
- `core/tests/test_data_manifest.py`：迁移摘要稳定性测试。
- `core/tests/test_agent_mail_preflight.py`：服务器 workspace、身份和测试邮件命令测试。
- `Dockerfile`：Web 与 Worker 两个构建 target。
- `.dockerignore`：排除本地数据、凭据、缓存和文档产物。
- `compose.yaml`：四服务轻量编排。
- `docker/worker-entrypoint.sh`：启动 D-Bus 与持久化 GNOME keyring 后执行 Worker 命令。
- `deploy/backup-postgres.sh`：原子 PostgreSQL 备份与 7 天轮转。
- `deploy/verify-backup.sh`：在临时数据库实际恢复并校验备份。
- `deploy/ticketwatch-backup.service`：一次性 systemd 备份服务。
- `deploy/ticketwatch-backup.timer`：每日备份定时器。
- `docs/server-deployment.md`：首次部署、授权、迁移、访问、升级和回滚手册。

### 修改文件

- `pyproject.toml`：增加 PostgreSQL、Redis、Gunicorn 和 WhiteNoise 依赖。
- `ticketwatch/settings.py`：环境化配置、生产保护、PostgreSQL、Redis 和 WhiteNoise。
- `ticketwatch/health.py`、`ticketwatch/urls.py`：保留轻量健康检查并增加详细私有状态端点。
- `core/models.py`：三级设置、租约、通知状态和 `RuntimeState`。
- `core/forms.py`、`core/views.py`、`templates/core/settings_form.html`：三级设置和逐任务重新调度。
- `templates/core/mail_form.html`：固定邮箱展示与测试按钮。
- `templates/core/dashboard.html`：Worker、数据库、Redis、邮件和备份状态摘要。
- `core/services/tasks.py`：按三级频率安排任务，并保护租约结果提交。
- `core/services/notifications.py`：系统异常通知、发送不确定状态和启动恢复。
- `core/services/agent_mail.py`：服务器 workspace 配置与发送超时的不确定分类。
- `core/scheduler.py`：本地线程复用公共 Worker 工作函数，唤醒同时兼容本地 Event 与 Redis。
- `core/management/commands/runlocal.py`：继续只供本机开发，不在生产启动。
- `.env.example`、`.gitignore`、`README.md`：生产变量、敏感文件和运行方式说明。
- 现有相关测试：更新字段名、workspace 和调度接口，不降低已有断言强度。

---

### Task 1: 建立生产配置边界和运行依赖

**Files:**
- Create: `ticketwatch/config.py`
- Create: `tests/test_production_config.py`
- Modify: `ticketwatch/settings.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`

**Interfaces:**
- Produces: `env_bool(env, name, default) -> bool`
- Produces: `build_database_config(env, base_dir) -> dict[str, dict]`
- Produces: `validate_production_env(env) -> None`
- Produces settings: `REDIS_URL`, `WORKER_SCAN_SECONDS`, `WORKER_LEASE_SECONDS`, `AGENTLY_WORKSPACE`

- [ ] **Step 1: 记录当前基线**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check . --exclude monitor.py
.venv/bin/python manage.py check
```

Expected: 现有 202 个测试通过，ruff 和 Django check 均退出 0。

- [ ] **Step 2: 写生产配置失败测试**

在 `tests/test_production_config.py` 写入：

```python
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

from ticketwatch.config import build_database_config, env_bool, validate_production_env


def test_production_requires_secret_and_postgres():
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        validate_production_env({"TICKETWATCH_ENV": "production"})


def test_production_database_never_falls_back_to_sqlite():
    env = {
        "TICKETWATCH_ENV": "production",
        "DJANGO_SECRET_KEY": "server-secret",
    }
    with pytest.raises(ImproperlyConfigured, match="POSTGRES_HOST"):
        build_database_config(env, Path("/app"))


def test_postgres_config_uses_explicit_values():
    env = {
        "TICKETWATCH_ENV": "production",
        "DJANGO_SECRET_KEY": "server-secret",
        "POSTGRES_HOST": "postgres",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "ticketwatch",
        "POSTGRES_USER": "ticketwatch",
        "POSTGRES_PASSWORD": "db-secret",
    }
    config = build_database_config(env, Path("/app"))["default"]
    assert config["ENGINE"] == "django.db.backends.postgresql"
    assert config["HOST"] == "postgres"
    assert config["CONN_MAX_AGE"] == 60


def test_env_bool_rejects_ambiguous_value():
    with pytest.raises(ImproperlyConfigured, match="DJANGO_DEBUG"):
        env_bool({"DJANGO_DEBUG": "sometimes"}, "DJANGO_DEBUG", False)
```

- [ ] **Step 3: 运行测试并确认缺少配置模块**

Run: `.venv/bin/pytest tests/test_production_config.py -v`

Expected: collection FAIL，错误包含 `No module named 'ticketwatch.config'`。

- [ ] **Step 4: 实现纯配置函数**

在 `ticketwatch/config.py` 实现以下行为：

```python
from django.core.exceptions import ImproperlyConfigured


def env_bool(env, name, default):
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean")


def validate_production_env(env):
    if env.get("TICKETWATCH_ENV") != "production":
        return
    if not env.get("DJANGO_SECRET_KEY"):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required in production")


def build_database_config(env, base_dir):
    production = env.get("TICKETWATCH_ENV") == "production"
    host = env.get("POSTGRES_HOST")
    if not host:
        if production:
            raise ImproperlyConfigured("POSTGRES_HOST is required in production")
        return {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": base_dir / "db.sqlite3"}}
    required = ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ImproperlyConfigured(f"missing PostgreSQL settings: {', '.join(missing)}")
    return {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env["POSTGRES_DB"],
            "USER": env["POSTGRES_USER"],
            "PASSWORD": env["POSTGRES_PASSWORD"],
            "HOST": host,
            "PORT": env.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
```

在 `ticketwatch/settings.py` 调用这些函数，设置 `DEBUG`、`SECRET_KEY`、`DATABASES`、`ALLOWED_HOSTS`、`REDIS_URL`、`WORKER_SCAN_SECONDS=10`、`WORKER_LEASE_SECONDS=300` 和 `AGENTLY_WORKSPACE=codex` 本地默认值。生产模式的默认 `DEBUG` 必须为 false。

在 `MIDDLEWARE` 的 SecurityMiddleware 后加入 `whitenoise.middleware.WhiteNoiseMiddleware`。

- [ ] **Step 5: 增加依赖和环境示例**

在 `pyproject.toml` 的运行依赖中增加：

```toml
"psycopg[binary]>=3.2,<4",
"redis>=6,<7",
"gunicorn>=23,<24",
"whitenoise>=6.9,<7",
```

在 `.env.example` 写出无真实秘密的完整键名：

```dotenv
TICKETWATCH_ENV=local
DJANGO_SECRET_KEY=replace-with-a-random-server-secret
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=ticketwatch
POSTGRES_USER=ticketwatch
POSTGRES_PASSWORD=replace-with-a-random-database-password
REDIS_URL=redis://redis:6379/0
WORKER_SCAN_SECONDS=10
WORKER_LEASE_SECONDS=300
AGENTLY_WORKSPACE=ticketwatch-server
AGENTLY_KEYRING_PASSWORD=replace-with-a-random-keyring-password
```

- [ ] **Step 6: 安装并验证**

Run:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/test_production_config.py tests/test_health.py -v
.venv/bin/python manage.py check
.venv/bin/ruff check ticketwatch tests/test_production_config.py
```

Expected: 全部通过；未设置 `TICKETWATCH_ENV=production` 时继续使用本地 SQLite。

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml ticketwatch/config.py ticketwatch/settings.py tests/test_production_config.py .env.example
git commit -m "feat: add production runtime configuration"
```

---

### Task 2: 实现三级检查频率和固定邮件配置

**Files:**
- Create: `core/services/scheduling.py`
- Create: `core/tests/test_scheduling.py`
- Create: `core/migrations/0005_server_runtime_fields.py`
- Modify: `core/models.py`
- Modify: `core/forms.py`
- Modify: `core/views.py`
- Modify: `core/services/tasks.py`
- Modify: `templates/core/settings_form.html`
- Modify: `templates/core/mail_form.html`
- Modify: `core/tests/test_models.py`
- Modify: `tests/test_mvp_flow.py`

**Interfaces:**
- Produces: `interval_seconds_for(show_date: date, now: datetime, setting: AppSetting) -> int`
- Produces: `next_check_at_for(show_date: date, now: datetime, setting: AppSetting) -> datetime`
- Consumes later: `MonitorTask.claim_token: UUID | None`, `MonitorTask.claim_expires_at: datetime | None`
- Produces notification choices: `Notification.Type.SYSTEM_ALERT`, `Notification.Status.NEEDS_REVIEW`

- [ ] **Step 1: 写频率边界和表单验证测试**

在 `core/tests/test_scheduling.py` 冻结上海时间并覆盖三档：

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from core.models import AppSetting
from core.services.scheduling import interval_seconds_for


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("show_date", "expected"),
    [(date(2026, 8, 18), 60), (date(2026, 8, 21), 300), (date(2026, 8, 30), 900)],
)
def test_interval_uses_editable_date_bands(show_date, expected):
    setting = AppSetting.get_solo()
    now = datetime(2026, 8, 17, 12, 0, tzinfo=SHANGHAI)
    assert interval_seconds_for(show_date, now, setting) == expected


@pytest.mark.django_db
def test_show_date_itself_stays_urgent():
    setting = AppSetting.get_solo()
    now = datetime(2026, 8, 20, 23, 59, tzinfo=SHANGHAI)
    assert interval_seconds_for(date(2026, 8, 20), now, setting) == 60
```

在 `tests/test_mvp_flow.py` 增加：保存 `urgent_window_hours=72`、`near_window_days=10` 和三个间隔后，不同日期的活动任务分别得到 `now + 60/300/900 秒`；任一间隔为 59 或近区间不大于紧急区间时，表单返回 200 且显示校验错误。

- [ ] **Step 2: 运行新测试并确认旧模型不支持字段**

Run: `.venv/bin/pytest core/tests/test_scheduling.py tests/test_mvp_flow.py -k 'editable_date_bands or show_date_itself or tier' -v`

Expected: FAIL，错误包含缺少 `urgent_window_hours` 或 `core.services.scheduling`。

- [ ] **Step 3: 扩展模型和迁移**

在 `AppSetting` 增加默认值和 `MinValueValidator`：

```python
urgent_window_hours = models.PositiveIntegerField(default=48, validators=[MinValueValidator(1)])
near_window_days = models.PositiveIntegerField(default=7, validators=[MinValueValidator(1)])
urgent_interval_seconds = models.PositiveIntegerField(default=60, validators=[MinValueValidator(60)])
near_interval_seconds = models.PositiveIntegerField(default=300, validators=[MinValueValidator(60)])
far_interval_seconds = models.PositiveIntegerField(default=900, validators=[MinValueValidator(60)])
```

本迁移保留 `poll_interval_seconds`，并用 `RunPython` 把其现有值复制到 `urgent_interval_seconds`。同一个数据迁移把单例邮件配置的现有收件地址更新为 `850634546@qq.com`，并把 `is_verified` 重置为 false、`verified_at` 清空，防止把 Mac workspace 的验证状态误带到服务器。同时为 `MonitorTask` 增加可空 `UUIDField claim_token` 和带索引的 `DateTimeField claim_expires_at`；为通知字段加入 `SYSTEM_ALERT` 与 `NEEDS_REVIEW` 选择；把 `AgentMailConfig.recipient_email` 改为固定默认 `850634546@qq.com` 且 `editable=False`。

- [ ] **Step 4: 实现区间计算**

在 `core/services/scheduling.py` 使用 `timezone.localtime(now)` 和上海本地目标日零点，严格实现：目标日当天或剩余小时小于等于紧急边界使用紧急间隔；否则剩余时间小于等于临近天数使用临近间隔；其余使用远期间隔。`next_check_at_for` 返回 `now + timedelta(seconds=interval_seconds_for(...))`。

- [ ] **Step 5: 修改设置表单和重新调度**

`AppSettingForm` 暴露五个新字段，在 `clean()` 中执行：

```python
if cleaned["near_window_days"] * 24 <= cleaned["urgent_window_hours"]:
    raise forms.ValidationError("临近区间必须大于紧急区间。")
```

`settings_edit` 在事务中锁定所有 `MONITORING` 任务，逐条调用 `next_check_at_for(task.show_date, now, setting)`，并在提交后调用 `wake_scheduler`。设置页展示两个边界和三个间隔，不再把一个固定轮询间隔描述为全局规则。

- [ ] **Step 6: 修改任务检查后的调度**

在 `core/services/tasks.py` 的成功未开票路径和错误退避路径中，用 `interval_seconds_for(current.show_date, now, AppSetting.get_solo())` 替换旧 `poll_interval_seconds`。失败退避仍取配置间隔与既有失败退避中的较大值。

- [ ] **Step 7: 固定邮件配置 UI**

移除收件地址编辑字段。`mail_edit` 只展示固定发件人、固定收件人、授权验证状态和“发送测试邮件”按钮；`mail_test` 继续 POST。迁移后模型值必须固定为 `850634546@qq.com`，服务器预检前会重新验证授权。

- [ ] **Step 8: 运行迁移和回归测试**

Run:

```bash
.venv/bin/python manage.py migrate
.venv/bin/pytest core/tests/test_scheduling.py core/tests/test_models.py tests/test_mvp_flow.py core/tests/test_checks.py core/tests/test_mail_config.py -v
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/ruff check core tests
```

Expected: 全部通过，migration 检查显示 `No changes detected`。

- [ ] **Step 9: 提交**

```bash
git add core/models.py core/forms.py core/views.py core/services/scheduling.py core/services/tasks.py core/migrations/0005_server_runtime_fields.py core/tests/test_scheduling.py core/tests/test_models.py core/tests/test_checks.py core/tests/test_mail_config.py tests/test_mvp_flow.py templates/core/settings_form.html templates/core/mail_form.html
git commit -m "feat: add adaptive monitoring intervals"
```

---

### Task 3: 用 PostgreSQL 租约安全领取任务

**Files:**
- Create: `core/services/leases.py`
- Create: `core/tests/test_leases.py`
- Modify: `core/services/tasks.py`
- Modify: `core/views.py`
- Modify: `core/services/notifications.py`
- Modify: `core/tests/test_checks.py`
- Modify: `core/tests/test_lifecycle.py`

**Interfaces:**
- Produces: `TaskClaim(task_id: UUID, token: UUID)`
- Produces: `claim_due_task(now: datetime | None = None, lease_seconds: int | None = None) -> TaskClaim | None`
- Produces: `claim_matches(task: MonitorTask, token: UUID | None) -> bool`
- Changes: `perform_check(task_id, now=None, claim_token=None) -> CheckRun | None`

- [ ] **Step 1: 写租约行为测试**

在 `core/tests/test_leases.py` 覆盖：最早到期任务先领取；未过期租约不能重复领取；过期租约可得到新 token；暂停任务不领取；领取只更新租约而不提前改变 `next_check_at`。

核心断言：

```python
claim = claim_due_task(now=now, lease_seconds=300)
assert claim.task_id == overdue.pk
overdue.refresh_from_db()
assert overdue.claim_token == claim.token
assert overdue.claim_expires_at == now + timedelta(seconds=300)
assert claim_due_task(now=now, lease_seconds=300) is None
```

在 `core/tests/test_checks.py` 增加：使用错误 token 提交检查结果时抛出 `StaleTaskClaim`，不创建 `CheckRun`；正确 token 完成后两个租约字段清空。

- [ ] **Step 2: 运行并确认缺少租约服务**

Run: `.venv/bin/pytest core/tests/test_leases.py core/tests/test_checks.py -k 'claim or lease' -v`

Expected: FAIL，缺少 `core.services.leases` 或 `perform_check(... claim_token=...)`。

- [ ] **Step 3: 实现领取事务**

在 `core/services/leases.py` 定义不可变 `TaskClaim` 和 `StaleTaskClaim`。`claim_due_task` 使用：

```python
with transaction.atomic():
    task = (
        MonitorTask.objects.select_for_update(skip_locked=True)
        .filter(status=MonitorTask.Status.MONITORING, next_check_at__lte=now)
        .filter(Q(claim_expires_at__isnull=True) | Q(claim_expires_at__lte=now))
        .order_by("next_check_at", "created_at", "pk")
        .first()
    )
```

找到任务后生成 `uuid.uuid4()`，把租约截止设为 `now + WORKER_LEASE_SECONDS` 并返回 `TaskClaim`。SQLite 测试环境不支持的 `skip_locked` 行为不得用后端分支伪造业务结果；测试单进程只验证查询语义，PostgreSQL 并发行为在容器集成测试中验证。

- [ ] **Step 4: 保护检查结果提交**

`perform_check` 接受可选 `claim_token`。传入 token 时，初始读取和最终锁定写入都必须匹配当前任务的 `claim_token`；否则抛出 `StaleTaskClaim`。所有成功、未开票、失败和终态路径在保存时清空 `claim_token`、`claim_expires_at`。

暂停、恢复、取消、到期和检测完成路径也清空租约。旧网络请求返回时，因为 token 已失效，不得覆盖这些用户操作。

- [ ] **Step 5: 验证 PostgreSQL 并发领取**

使用临时 PostgreSQL 测试数据库启动两个线程，同时调用 `claim_due_task`。断言只有一个线程得到同一任务，另一个得到 `None` 或不同任务。该测试标记 `@pytest.mark.postgres`，普通 SQLite 测试跳过；Compose 集成阶段必须运行。

- [ ] **Step 6: 运行局部与完整测试**

Run:

```bash
.venv/bin/pytest core/tests/test_leases.py core/tests/test_checks.py core/tests/test_lifecycle.py core/tests/test_expiry.py -v
.venv/bin/pytest -q
.venv/bin/ruff check core
```

Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add core/services/leases.py core/services/tasks.py core/services/notifications.py core/views.py core/tests/test_leases.py core/tests/test_checks.py core/tests/test_lifecycle.py
git commit -m "feat: lease due monitoring tasks"
```

---

### Task 4: 增加 Redis 唤醒和独立 Worker 进程

**Files:**
- Create: `core/services/coordination.py`
- Create: `core/worker.py`
- Create: `core/management/commands/runworker.py`
- Create: `core/tests/test_worker.py`
- Modify: `core/scheduler.py`
- Modify: `core/management/commands/runlocal.py`
- Modify: `core/tests/test_scheduler.py`

**Interfaces:**
- Produces: `notify_worker() -> bool`
- Produces: `wait_for_worker(timeout_seconds: int, stop_event: threading.Event | None = None) -> bool`
- Produces: `run_due_work(now=None) -> dict[str, int | bool]`
- Produces: `WorkerLoop.run_once(now=None) -> dict[str, int | bool]`
- Produces: `WorkerLoop.run_forever() -> None`, `WorkerLoop.stop() -> None`

- [ ] **Step 1: 写 Redis 降级和 Worker 顺序测试**

在 `core/tests/test_worker.py` 写入以下场景：

```python
def test_notify_worker_returns_false_when_redis_is_unconfigured(settings):
    settings.REDIS_URL = ""
    assert notify_worker() is False


def test_worker_runs_expiry_mail_then_one_claim(mocker):
    calls = []
    mocker.patch("core.worker.expire_due_task", side_effect=lambda now=None: calls.append("expire") or False)
    mocker.patch("core.worker.dispatch_due_notifications", side_effect=lambda now=None: calls.append("mail") or 1)
    claim = TaskClaim(task_id=uuid.uuid4(), token=uuid.uuid4())
    mocker.patch("core.worker.claim_due_task", side_effect=lambda now=None: calls.append("claim") or claim)
    check = mocker.patch("core.worker.perform_check", side_effect=lambda *args, **kwargs: calls.append("check"))
    result = run_due_work(now=timezone.now())
    assert calls == ["expire", "mail", "claim", "check"]
    assert result == {"expired": False, "notifications": 1, "checked": True}
    check.assert_called_once_with(claim.task_id, now=mocker.ANY, claim_token=claim.token)
```

另写测试：Redis `ConnectionError` 时 `notify_worker` 只记录异常类且返回 false；`wait_for_worker` 在 Redis 故障后使用有上限的本地等待；`WorkerLoop.stop()` 能打断等待。

- [ ] **Step 2: 运行并确认新 Worker 不存在**

Run: `.venv/bin/pytest core/tests/test_worker.py -v`

Expected: collection FAIL，缺少 `core.worker` 或 `core.services.coordination`。

- [ ] **Step 3: 实现 Redis 协调**

`notify_worker` 使用 `redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)`，向固定列表 `ticketwatch:worker:wake` 执行 `LPUSH` 和 `LTRIM 0 0`。`wait_for_worker` 使用 `BRPOP`，超时返回 false。没有 Redis URL 或发生 `RedisError` 时，不抛给调用者，只记录异常类，并调用传入的 `stop_event.wait(timeout_seconds)`；未传 Event 时使用模块内短期 Event。这样 Redis 降级时既不忙循环，也能被 Worker 停止信号打断。

- [ ] **Step 4: 实现公共工作循环**

把现有 `run_due_work` 移到 `core/worker.py`，改为调用 `claim_due_task` 后把 token 传入 `perform_check`。`WorkerLoop.run_forever` 每轮执行一次；有工作时立即下一轮，无工作时调用 `wait_for_worker(settings.WORKER_SCAN_SECONDS, self._stop_event)`。`stop()` 设置停止 Event 并调用一次 `notify_worker()`，使 Redis 阻塞等待也立即返回。

- [ ] **Step 5: 实现管理命令和信号退出**

`runworker` 创建 `WorkerLoop`，为 `SIGTERM` 和 `SIGINT` 注册只调用 `loop.stop()` 的处理函数，然后执行 `run_forever()`。正常停止退出码为 0；未捕获异常由命令退出非零，让 Docker 重启。

- [ ] **Step 6: 保留本机 runlocal**

`LocalScheduler.tick()` 导入并调用 `core.worker.run_due_work`。`wake_scheduler()` 先唤醒当前进程内 Event，再调用 `notify_worker()`；这样本机模式无 Redis 仍工作，服务器 Web 能唤醒独立 Worker。更新 mock 路径和原有顺序测试。

- [ ] **Step 7: 运行验证**

Run:

```bash
.venv/bin/pytest core/tests/test_worker.py core/tests/test_scheduler.py tests/test_mvp_flow.py -v
.venv/bin/python manage.py runworker --help
.venv/bin/ruff check core
```

Expected: 全部通过；`runworker --help` 显示独立后台 Worker 命令。

- [ ] **Step 8: 提交**

```bash
git add core/services/coordination.py core/worker.py core/management/commands/runworker.py core/scheduler.py core/management/commands/runlocal.py core/tests/test_worker.py core/tests/test_scheduler.py
git commit -m "feat: run monitoring in a standalone worker"
```

---

### Task 5: 补齐异常告警和不确定邮件恢复

**Files:**
- Modify: `core/services/agent_mail.py`
- Modify: `core/services/notifications.py`
- Modify: `core/services/tasks.py`
- Modify: `core/worker.py`
- Modify: `core/tests/test_agent_mail.py`
- Modify: `core/tests/test_notifications.py`
- Modify: `core/tests/test_checks.py`

**Interfaces:**
- Produces: `AgentMailUncertainError`
- Produces: `mark_uncertain_sending_notifications() -> int`
- Produces: `Notification.Type.SYSTEM_ALERT`
- Produces: `Notification.Status.NEEDS_REVIEW`

- [ ] **Step 1: 写不确定发送测试**

增加以下断言：

```python
def test_cli_timeout_is_uncertain_not_retryable(mocker):
    mocker.patch("core.services.agent_mail.shutil.which", return_value="agently-cli")
    mocker.patch("core.services.agent_mail.subprocess.run", side_effect=subprocess.TimeoutExpired("agently-cli", 30))
    with pytest.raises(AgentMailUncertainError, match="agent-mail-result-unknown"):
        send_agent_mail(RECIPIENT, "测试", "正文")


@pytest.mark.django_db
def test_worker_startup_parks_preexisting_sending_notification(opening_notification):
    opening_notification.status = Notification.Status.SENDING
    opening_notification.save(update_fields=["status"])
    assert mark_uncertain_sending_notifications() == 1
    opening_notification.refresh_from_db()
    assert opening_notification.status == Notification.Status.NEEDS_REVIEW
    assert opening_notification.next_attempt_at is None
```

再增加：第五次结构错误把任务置为 `ERROR` 并只创建一个 `SYSTEM_ALERT`；临时网络错误和限流错误继续退避且不创建系统告警；恢复异常任务不会创建第二条系统告警。

- [ ] **Step 2: 运行测试确认现状错误**

Run: `.venv/bin/pytest core/tests/test_agent_mail.py core/tests/test_notifications.py core/tests/test_checks.py -k 'uncertain or system_alert or fifth_structure' -v`

Expected: FAIL，超时仍被分类为 temporary，且缺少恢复函数或告警类型。

- [ ] **Step 3: 实现不确定状态**

`subprocess.TimeoutExpired` 改为抛出 `AgentMailUncertainError("agent-mail-result-unknown")`。`deliver_notification` 捕获它并把当前 `SENDING` 记录原子更新为 `NEEDS_REVIEW`，不设置 `next_attempt_at`。

`mark_uncertain_sending_notifications` 在 Worker 启动时一次性把遗留 `SENDING` 更新为 `NEEDS_REVIEW`，错误摘要固定为 `agent-mail-result-unknown-after-restart`。`WorkerLoop.run_forever` 进入循环前调用一次该函数。

- [ ] **Step 4: 实现系统异常通知**

只有连续第五次 `STRUCTURE_ERROR` 或 `CONFIG_ERROR` 把任务转为 `ERROR`，并在同一事务中 `get_or_create` 一条 `SYSTEM_ALERT`。临时网络和限流错误按已有上限退避但不因为次数自动停止。

`_build_message` 为 `SYSTEM_ALERT` 生成固定主题 `[TicketWatch] 监控任务需要处理`，正文只含电影、日期、城市、影院、错误类别和任务详情路径，不包含原始响应。

- [ ] **Step 5: 运行回归测试**

Run:

```bash
.venv/bin/pytest core/tests/test_agent_mail.py core/tests/test_notifications.py core/tests/test_checks.py core/tests/test_lifecycle.py -v
.venv/bin/pytest -q
.venv/bin/ruff check core
```

Expected: 全部通过；已有开票和到期邮件重试测试不变。

- [ ] **Step 6: 提交**

```bash
git add core/services/agent_mail.py core/services/notifications.py core/services/tasks.py core/worker.py core/tests/test_agent_mail.py core/tests/test_notifications.py core/tests/test_checks.py
git commit -m "feat: make notification recovery duplicate safe"
```

---

### Task 6: 增加运行心跳和私有状态页面

**Files:**
- Create: `core/migrations/0006_runtime_state.py`
- Create: `core/services/runtime_health.py`
- Create: `core/management/commands/mark_backup_success.py`
- Create: `core/tests/test_runtime_health.py`
- Modify: `core/models.py`
- Modify: `core/worker.py`
- Modify: `ticketwatch/health.py`
- Modify: `ticketwatch/urls.py`
- Modify: `core/views.py`
- Modify: `templates/core/dashboard.html`
- Modify: `tests/test_health.py`

**Interfaces:**
- Produces singleton: `RuntimeState(worker_started_at, worker_heartbeat_at, last_backup_at, last_backup_name)`
- Produces: `record_worker_heartbeat(started=False, now=None) -> None`
- Produces: `collect_runtime_status(now=None) -> dict`
- Produces endpoint: `GET /statusz`

- [ ] **Step 1: 写心跳与去敏状态测试**

测试 `collect_runtime_status` 返回固定顶层键：

```python
assert status.keys() == {
    "status", "database", "redis", "worker", "mail", "tasks", "last_check_at", "backup", "disk"
}
assert status["tasks"].keys() == {"monitoring", "error", "detected", "pending_notifications"}
assert "password" not in json.dumps(status).lower()
assert "token" not in json.dumps(status).lower()
```

冻结时间，断言 30 秒内的心跳为 `ok`，超过 `max(WORKER_SCAN_SECONDS * 3, 30)` 为 `stale`。Redis ping 失败时总体状态为 `degraded`，但数据库任务统计仍返回。

- [ ] **Step 2: 运行测试确认缺少 RuntimeState**

Run: `.venv/bin/pytest core/tests/test_runtime_health.py tests/test_health.py -v`

Expected: FAIL，缺少 `RuntimeState` 或 `/statusz` 返回 404。

- [ ] **Step 3: 实现模型、迁移和心跳**

新增 `RuntimeState(SingletonModel)`，四个字段均可空，`last_backup_name` 最大 255。Worker 启动记录 `worker_started_at`，每轮开始更新 `worker_heartbeat_at`。心跳写入失败应让 Worker 退出并由 Docker 重启，因为数据库已经不可用。

- [ ] **Step 4: 实现详细状态聚合**

数据库用 `SELECT 1` 检查；Redis 用 1 秒超时 `PING`；任务和待通知数量直接从 PostgreSQL 聚合；最近检查取 `CheckRun.finished_at` 最大值；磁盘用 `shutil.disk_usage(settings.BASE_DIR)`。只返回状态、数量、ISO 时间和字节数，不返回异常文本、环境变量或连接字符串。

保留 `/healthz` 为无敏感轻量存活响应 `{"status":"ok"}`，新增 `/statusz` 返回详细 JSON。Dashboard 调用同一服务展示摘要。

- [ ] **Step 5: 实现备份状态命令**

`python manage.py mark_backup_success --name ticketwatch-20260817T020000Z.sql.gz` 校验参数只能是文件名而不能包含 `/`，然后写入 `last_backup_at` 和 `last_backup_name`。测试路径穿越名称被拒绝。

- [ ] **Step 6: 运行验证并提交**

```bash
.venv/bin/python manage.py migrate
.venv/bin/pytest core/tests/test_runtime_health.py tests/test_health.py -v
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/ruff check core ticketwatch
git add core/models.py core/migrations/0006_runtime_state.py core/services/runtime_health.py core/management/commands/mark_backup_success.py core/worker.py ticketwatch/health.py ticketwatch/urls.py core/views.py templates/core/dashboard.html core/tests/test_runtime_health.py tests/test_health.py
git commit -m "feat: expose private runtime health status"
```

Expected: 测试通过，migration 检查无变化，提交成功。

---

### Task 7: 建立 SQLite 到 PostgreSQL 的可核对迁移流程

**Files:**
- Create: `core/services/data_manifest.py`
- Create: `core/management/commands/data_manifest.py`
- Create: `core/tests/test_data_manifest.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `build_data_manifest() -> dict`
- Produces command: `python manage.py data_manifest --output migration-data/local.manifest.json`

- [ ] **Step 1: 写稳定清单测试**

创建两条任务、检查和通知，调用两次 `build_data_manifest`，断言结果完全相同并包含：

```python
assert manifest["schema_version"] == 1
assert manifest["models"]["core.monitortask"]["count"] == 2
assert len(manifest["models"]["core.monitortask"]["sha256"]) == 64
assert "runtimestate" not in manifest["models"]
```

更新一个任务展示字段后摘要必须改变；更新 `RuntimeState.worker_heartbeat_at` 后摘要不得改变。

- [ ] **Step 2: 运行确认命令不存在**

Run: `.venv/bin/pytest core/tests/test_data_manifest.py -v`

Expected: FAIL，缺少 `core.services.data_manifest`。

- [ ] **Step 3: 实现跨数据库稳定摘要**

固定模型顺序为 `AppSetting`、`AgentMailConfig`、`MonitorTask`、`CheckRun`、`Notification`。每个模型按主键排序，读取所有 concrete 且非 auto-created 字段，用 `DjangoJSONEncoder`、`ensure_ascii=False`、`sort_keys=True` 和紧凑 separators 序列化，再计算 SHA-256。结果只包含 schema version、每个模型的 count 和 sha256，不包含原始业务数据。

- [ ] **Step 4: 实现原子输出命令**

命令要求 `--output`，先写同目录权限 `0600` 的临时文件，`fsync` 成功后 `os.replace` 到目标路径。输出路径父目录必须已存在。命令标准输出只打印最终路径，不打印 manifest 内容。

- [ ] **Step 5: 忽略迁移产物并验证**

在 `.gitignore` 增加：

```gitignore
migration-data/
*.manifest.json
```

Run:

```bash
.venv/bin/pytest core/tests/test_data_manifest.py -v
mkdir -p migration-data
.venv/bin/python manage.py data_manifest --output migration-data/local.manifest.json
test -s migration-data/local.manifest.json
.venv/bin/ruff check core
```

Expected: 测试通过，清单文件非空且未被 Git 跟踪。

- [ ] **Step 6: 提交**

```bash
git add core/services/data_manifest.py core/management/commands/data_manifest.py core/tests/test_data_manifest.py .gitignore
git commit -m "feat: add migration data manifests"
```

---

### Task 8: 构建轻量 Web、Worker、PostgreSQL 和 Redis 容器

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yaml`
- Create: `docker/worker-entrypoint.sh`
- Modify: `.env.example`
- Modify: `.gitignore`

**Interfaces:**
- Produces image targets: `web`, `worker`
- Produces services: `web`, `worker`, `postgres`, `redis`
- Consumes: `python manage.py runworker`, `/healthz`, `/home/ticketwatch` Agent Mail state

- [ ] **Step 1: 写 Docker 构建结构**

`Dockerfile` 使用 `python:3.12-slim-bookworm` 基础层，创建 UID/GID 10001 的非 root 用户，复制项目并 `pip install --no-cache-dir .`。

`web` target 在构建时执行 `collectstatic --noinput`，默认命令固定为：

```text
gunicorn ticketwatch.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 2 --timeout 60 --access-logfile - --error-logfile -
```

`worker` target 额外安装 `nodejs`、`npm`、`dbus-x11`、`gnome-keyring`、`libsecret-1-0`，再安装 `@tencent-qqmail/agently-cli@1.0.15`，入口为 `docker/worker-entrypoint.sh`，命令为 `python manage.py runworker`。

两个 target 都使用 exec 形式的 `CMD`，镜像中不复制 `.env`、SQLite、migration export 或本机 Agent Mail 目录。

- [ ] **Step 2: 实现受保护的 keyring 入口**

脚本内容固定为以下结构，且不得增加 `set -x` 或输出密码/keyring 环境：

```sh
#!/bin/sh
set -eu

: "${AGENTLY_KEYRING_PASSWORD:?AGENTLY_KEYRING_PASSWORD is required}"
export HOME=/home/ticketwatch
export XDG_RUNTIME_DIR=/tmp/ticketwatch-runtime
mkdir -p "$XDG_RUNTIME_DIR" "$HOME/.local/share/keyrings" "$HOME/.agently-cli"
chmod 700 "$XDG_RUNTIME_DIR" "$HOME/.local/share/keyrings" "$HOME/.agently-cli"

exec dbus-run-session -- sh -eu -c '
  export HOME=/home/ticketwatch
  eval "$(printf "%s" "$AGENTLY_KEYRING_PASSWORD" | gnome-keyring-daemon --unlock --components=secrets)"
  exec "$@"
' sh "$@"
```

- [ ] **Step 3: 写 Compose 四服务**

`compose.yaml` 必须满足：

- `web` 仅映射 `127.0.0.1:8000:8000`，内存上限 384 MiB；
- `worker` 不映射端口，内存上限 384 MiB，挂载 `../data/agently:/home/ticketwatch`；
- `postgres:16-bookworm` 不映射端口，挂载 `../data/postgres:/var/lib/postgresql/data`，内存上限 512 MiB，`max_connections=20`；
- `redis:7-alpine` 不映射端口，挂载 `../data/redis:/data`，使用 `--maxmemory 64mb --maxmemory-policy noeviction --appendonly yes`，内存上限 96 MiB；
- 所有服务 `restart: unless-stopped`；
- PostgreSQL healthcheck 使用 `pg_isready -U ticketwatch -d ticketwatch`，Redis 使用 `redis-cli ping`，Web 使用 Python `urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)`；
- Docker json-file 日志为 `max-size: 10m`、`max-file: 3`；
- Web 和 Worker 读取 `../.env`；
- `env_file` 和三个 bind mount 源目录分别由 `TICKETWATCH_ENV_FILE`、`POSTGRES_DATA_DIR`、`REDIS_DATA_DIR`、`AGENTLY_DATA_DIR` 插值；服务器 `.env` 将它们固定到 `/opt/ticketwatch/`，集成测试将它们固定到独立临时目录；
- Web 等待 PostgreSQL 和 Redis healthy，Worker 等待三者 healthy；
- 不声明 Nginx、Certbot、Celery、OpenClaw 服务。

在 `.env.example` 增加 `TICKETWATCH_ENV_FILE=.env.example` 以及三个 `/tmp/ticketwatch-example-*` 示例目录。在 `.gitignore` 增加 `.env.*` 并用 `!.env.example` 保留示例，保证 `.env.integration` 永远不被提交。

- [ ] **Step 4: 排除敏感构建上下文**

`.dockerignore` 至少包含 `.git`、`.env`、`.venv`、`db.sqlite3`、`*.sqlite3`、`.ticketwatch.key`、`backups`、`migration-data`、`logs`、`output`、缓存目录和 `staticfiles`。

- [ ] **Step 5: 静态检查 Compose**

Run:

```bash
TICKETWATCH_ENV_FILE=.env.example POSTGRES_DATA_DIR=/tmp/ticketwatch-plan-postgres REDIS_DATA_DIR=/tmp/ticketwatch-plan-redis AGENTLY_DATA_DIR=/tmp/ticketwatch-plan-agently docker compose --env-file .env.example config --quiet
TICKETWATCH_ENV_FILE=.env.example POSTGRES_DATA_DIR=/tmp/ticketwatch-plan-postgres REDIS_DATA_DIR=/tmp/ticketwatch-plan-redis AGENTLY_DATA_DIR=/tmp/ticketwatch-plan-agently docker compose --env-file .env.example config | rg '127.0.0.1:8000'
TICKETWATCH_ENV_FILE=.env.example POSTGRES_DATA_DIR=/tmp/ticketwatch-plan-postgres REDIS_DATA_DIR=/tmp/ticketwatch-plan-redis AGENTLY_DATA_DIR=/tmp/ticketwatch-plan-agently docker compose --env-file .env.example config | rg -n '5432:5432|6379:6379' && exit 1 || true
```

Expected: Compose 配置有效；只出现回环 Web 映射；没有数据库或 Redis 的宿主机端口映射。

- [ ] **Step 6: 构建并执行镜像检查**

Run:

```bash
docker compose --env-file .env.example build web worker
docker compose --env-file .env.example run --rm web python manage.py check --deploy
docker compose --env-file .env.example run --rm worker agently-cli --version
```

Expected: 两个镜像构建成功；Django deploy check 不含 ERROR；CLI 输出 `1.0.15`。

- [ ] **Step 7: 提交**

```bash
git add Dockerfile .dockerignore compose.yaml docker/worker-entrypoint.sh .env.example .gitignore
git commit -m "build: add lightweight server containers"
```

---

### Task 9: 增加服务器 Agent Mail 预检

**Files:**
- Create: `core/management/commands/agent_mail_preflight.py`
- Create: `core/tests/test_agent_mail_preflight.py`
- Modify: `core/services/agent_mail.py`
- Modify: `core/tests/test_agent_mail.py`

**Interfaces:**
- Consumes setting: `AGENTLY_WORKSPACE`
- Produces command: `python manage.py agent_mail_preflight [--send-test]`

- [ ] **Step 1: 写 workspace 和预检命令测试**

用 `override_settings(AGENTLY_WORKSPACE="ticketwatch-server")` 断言 `_run` 传给子进程的环境包含该值而不是硬编码 `codex`。预检身份不匹配时退出非零，且输出不包含 CLI stderr。`--send-test` 成功时把 `AgentMailConfig.is_verified` 设为 true；失败时设为 false 并保存固定去敏错误码。

- [ ] **Step 2: 运行确认硬编码导致失败**

Run: `.venv/bin/pytest core/tests/test_agent_mail_preflight.py core/tests/test_agent_mail.py -v`

Expected: FAIL，环境仍为 `codex` 或命令不存在。

- [ ] **Step 3: 环境化 workspace**

`core/services/agent_mail.py` 从 `django.conf.settings.AGENTLY_WORKSPACE` 读取 workspace。`verify_agent_mail` 仍强制主邮箱为 `mijiatong@agent.qq.com`，不得仅按 alias 存在判断。

- [ ] **Step 4: 实现预检命令**

无 `--send-test` 时只执行身份检查并打印固定成功语句。传入 `--send-test` 时，先把配置标为未验证，再调用 `test_agent_mail_config` 向固定收件人发送真实测试邮件；成功后写 `verified_at`，失败时只保存异常类映射后的安全错误码，并抛 `CommandError`。

- [ ] **Step 5: 验证并提交**

```bash
.venv/bin/pytest core/tests/test_agent_mail_preflight.py core/tests/test_agent_mail.py core/tests/test_mail_config.py -v
.venv/bin/ruff check core
git add core/management/commands/agent_mail_preflight.py core/services/agent_mail.py core/tests/test_agent_mail_preflight.py core/tests/test_agent_mail.py
git commit -m "feat: add server agent mail preflight"
```

Expected: 测试和 lint 通过，提交成功。

---

### Task 10: 实现数据库备份、恢复演练和定时器

**Files:**
- Create: `deploy/backup-postgres.sh`
- Create: `deploy/verify-backup.sh`
- Create: `deploy/ticketwatch-backup.service`
- Create: `deploy/ticketwatch-backup.timer`

**Interfaces:**
- Consumes: `/opt/ticketwatch/.env`, Compose service `postgres`, command `mark_backup_success`
- Produces filename pattern: `/opt/ticketwatch/data/backups/ticketwatch-YYYYMMDDTHHMMSSZ.sql.gz`

- [ ] **Step 1: 实现原子备份脚本**

脚本使用 Bash 的 `pipefail`，固定并验证目录，核心实现如下：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

WORK_DIR=/opt/ticketwatch/app
BACKUP_DIR=/opt/ticketwatch/data/backups
ENV_FILE=/opt/ticketwatch/.env
case "$WORK_DIR:$BACKUP_DIR" in
  /opt/ticketwatch/*:/opt/ticketwatch/*) ;;
  *) exit 2 ;;
esac
mkdir -p "$BACKUP_DIR"
set -a
. "$ENV_FILE"
set +a
cd "$WORK_DIR"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL_PATH="$BACKUP_DIR/ticketwatch-$STAMP.sql.gz"
TEMP_PATH=$(mktemp "$BACKUP_DIR/.ticketwatch-backup.XXXXXX")
cleanup() {
  if [[ -n "${TEMP_PATH:-}" && -f "$TEMP_PATH" ]]; then
    rm -f -- "$TEMP_PATH"
  fi
}
trap cleanup EXIT HUP INT TERM

docker compose --env-file "$ENV_FILE" exec -T postgres \
  pg_dump --format=custom --no-owner --no-acl \
  --username "$POSTGRES_USER" "$POSTGRES_DB" | gzip -c >"$TEMP_PATH"
test -s "$TEMP_PATH"
mv -- "$TEMP_PATH" "$FINAL_PATH"
TEMP_PATH=
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -mtime +7 -delete
docker compose --env-file "$ENV_FILE" exec -T web \
  python manage.py mark_backup_success --name "${FINAL_PATH##*/}"
```

失败时 trap 只删除已验证位于备份目录内的临时文件；最终文件原子改名后才轮转旧备份和记录成功状态。

- [ ] **Step 2: 实现隔离恢复演练脚本**

`verify-backup.sh` 接收一个显式备份绝对路径，用 `realpath` 验证其位于备份目录且匹配命名规则。核心恢复过程如下：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

WORK_DIR=/opt/ticketwatch/app
BACKUP_DIR=/opt/ticketwatch/data/backups
ENV_FILE=/opt/ticketwatch/.env
BACKUP_REAL=$(realpath "${1:?backup path is required}")
case "$BACKUP_REAL" in
  "$BACKUP_DIR"/ticketwatch-*.sql.gz) ;;
  *) exit 2 ;;
esac
set -a
. "$ENV_FILE"
set +a
cd "$WORK_DIR"

RESTORE_DB="ticketwatch_restore_$(date -u +%Y%m%d%H%M%S)_$$"
cleanup() {
  docker compose --env-file "$ENV_FILE" exec -T postgres \
    dropdb --if-exists --username "$POSTGRES_USER" "$RESTORE_DB" >/dev/null
}
trap cleanup EXIT HUP INT TERM
docker compose --env-file "$ENV_FILE" exec -T postgres \
  createdb --username "$POSTGRES_USER" "$RESTORE_DB"
gzip -dc "$BACKUP_REAL" | docker compose --env-file "$ENV_FILE" exec -T postgres \
  pg_restore --exit-on-error --no-owner --no-acl \
  --username "$POSTGRES_USER" --dbname "$RESTORE_DB"
docker compose --env-file "$ENV_FILE" exec -T postgres \
  psql --username "$POSTGRES_USER" --dbname "$RESTORE_DB" --tuples-only --command \
  'SELECT COUNT(*) FROM django_migrations; SELECT COUNT(*) FROM core_monitortask; SELECT COUNT(*) FROM core_checkrun; SELECT COUNT(*) FROM core_notification; SELECT COUNT(*) FROM core_appsetting; SELECT COUNT(*) FROM core_agentmailconfig;'
```

trap 只删除名称由脚本本轮生成的临时数据库，绝不删除生产数据库。

- [ ] **Step 3: 增加 systemd 单元**

Service 使用：

```ini
[Service]
Type=oneshot
User=admin
ExecStart=/opt/ticketwatch/app/deploy/backup-postgres.sh
```

Timer 使用 `OnCalendar=*-*-* 03:20:00 Asia/Shanghai`、`Persistent=true` 和 `RandomizedDelaySec=300`。安装后由 `systemctl enable --now ticketwatch-backup.timer` 启用。

- [ ] **Step 4: 执行静态检查**

Run:

```bash
bash -n deploy/backup-postgres.sh
bash -n deploy/verify-backup.sh
systemd-analyze verify deploy/ticketwatch-backup.service deploy/ticketwatch-backup.timer
```

Expected: shell 语法通过，systemd 单元无 ERROR。

- [ ] **Step 5: 提交**

```bash
git add deploy/backup-postgres.sh deploy/verify-backup.sh deploy/ticketwatch-backup.service deploy/ticketwatch-backup.timer
git commit -m "ops: add postgres backup and restore checks"
```

---

### Task 11: 编写可复制的服务器部署与回滚手册

**Files:**
- Create: `docs/server-deployment.md`
- Modify: `README.md`

**Interfaces:**
- Documents: 首次安装、SQLite 导出、PostgreSQL 导入、Agent Mail 授权、SSH 隧道、备份、升级和回滚。

- [ ] **Step 1: 写部署前检查命令**

文档固定包含：

```bash
cat /etc/os-release
nproc
free -h
df -h /
docker --version
sudo dnf install -y docker-compose-plugin
docker compose version
```

再检查阿里云安全组只有 22/TCP，不指导开放 80、443 或 8000。

- [ ] **Step 2: 写本地 SQLite 安全导出步骤**

固定顺序为停止 `runlocal`、复制 SQLite、迁移本地 schema、生成 manifest、使用 `dumpdata` 导出五个业务模型：

```bash
mkdir -p migration-data
cp -p db.sqlite3 migration-data/db-before-server.sqlite3
.venv/bin/python manage.py migrate
.venv/bin/python manage.py data_manifest --output migration-data/local.manifest.json
.venv/bin/python manage.py dumpdata core.AppSetting core.AgentMailConfig core.MonitorTask core.CheckRun core.Notification --indent 2 --output migration-data/core-data.json
```

文档要求对三个文件执行 SHA-256 并记录结果。

- [ ] **Step 3: 写服务器目录、权限和启动步骤**

包含创建 `/opt/ticketwatch/{app,data/postgres,data/redis,data/agently,data/backups,logs}`、克隆指定 Git 提交、复制 `.env.example` 为 `/opt/ticketwatch/.env`、生成随机 Django/数据库/keyring 密码、设置 `.env` 为 0600，以及为 bind mount 设置明确 UID 权限。

启动顺序必须是 PostgreSQL/Redis、migration、`loaddata`、manifest 比对、Web、Agent Mail 授权预检、Worker。

- [ ] **Step 4: 写 Agent Mail 与 SSH 操作**

文档包含：

```bash
docker compose --env-file /opt/ticketwatch/.env run --rm worker agently-cli auth login
docker compose --env-file /opt/ticketwatch/.env run --rm worker python manage.py agent_mail_preflight --send-test
ssh -N -L 18000:127.0.0.1:8000 admin@47.116.69.108
```

浏览器只访问 `http://127.0.0.1:18000`。明确说明 SSH 中断不停止 Worker。

- [ ] **Step 5: 写升级和回滚 Runbook**

升级前记录 Git SHA 并执行备份；检出明确新 SHA、构建、migration、重启和健康检查。应用回滚只切回上一个 SHA 和镜像；数据库只有在确认数据破坏时才恢复，恢复前先保留故障现场备份。

- [ ] **Step 6: 更新 README 并检查命令**

README 保留本机运行方式，新增“私有服务器部署”入口链接，不再把 Docker/PostgreSQL/Redis 一概列为永久不支持。检查文档中没有真实密码、OAuth token 或将端口开放公网的命令。

Run:

```bash
rg -n '47\.116\.69\.108|127\.0\.0\.1:8000|ticketwatch-server|data_manifest' docs/server-deployment.md
rg -n -i '(oauth|access)[_-]?token[[:space:]]*=' docs/server-deployment.md && exit 1 || true
git diff --check
```

Expected: 必需内容存在，无真实秘密，Markdown 无空白错误。

- [ ] **Step 7: 提交**

```bash
git add docs/server-deployment.md README.md
git commit -m "docs: add private server operations guide"
```

---

### Task 12: 完整自动化回归和 Compose 集成验收

**Files:**
- Modify only if a failing test proves a defect in files introduced by Tasks 1–11.

**Interfaces:**
- Verifies all previously produced interfaces together.

- [ ] **Step 1: 运行完整本机质量门**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check . --exclude monitor.py
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
git diff --check
```

Expected: 全部测试通过，lint/check 退出 0，无未生成 migration。

- [ ] **Step 2: 启动隔离 Compose 集成环境**

创建被 `.gitignore` 排除的 `.env.integration`，使用 `TICKETWATCH_ENV_FILE=.env.integration`、`TICKETWATCH_ENV=production`、`DJANGO_SECRET_KEY=integration-only-secret`、`POSTGRES_DB=ticketwatch_integration`、`POSTGRES_USER=ticketwatch`、`POSTGRES_PASSWORD=integration-only-password`、`AGENTLY_KEYRING_PASSWORD=integration-only-keyring`，并把三个数据目录固定到 `/tmp/ticketwatch-integration-data/postgres`、`/tmp/ticketwatch-integration-data/redis`、`/tmp/ticketwatch-integration-data/agently`。先确认该目录不是符号链接且不包含业务文件，再启动 PostgreSQL、Redis、Web；执行 migration。不得挂载本机真实 `db.sqlite3` 或 Agent Mail 目录。

Run:

```bash
docker compose --env-file .env.integration up -d postgres redis
docker compose --env-file .env.integration run --rm web python manage.py migrate
docker compose --env-file .env.integration up -d web
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/statusz
```

Expected: healthz 为 `ok`；Worker 未启动时 statusz 明确报告 worker stale，而不是伪装正常。

- [ ] **Step 3: 在 PostgreSQL 上运行并发租约测试**

Run:

```bash
docker compose --env-file .env.integration run --rm web pytest -m postgres core/tests/test_leases.py -v
```

Expected: 两个并发领取者不会得到同一任务。

- [ ] **Step 4: 验证 Redis 丢失不丢任务**

在测试库创建一条到期任务，停止 Redis，保持 Worker 运行一个扫描周期，再启动 Redis。断言任务仍在 PostgreSQL，Worker 日志只有去敏的 Redis 异常类；Redis 恢复后任务被检查或保留为可重试状态。

- [ ] **Step 5: 验证服务重启恢复**

分别重启 Web、Worker 和整套 Compose。验证 Web 重启期间 Worker 心跳继续；Worker 重启后 `SENDING` 转为 `NEEDS_REVIEW`、过期租约可回收；整套重启后四服务恢复 healthy。

- [ ] **Step 6: 清理隔离环境**

先用 `docker compose --env-file .env.integration ps` 确认项目名和服务，再执行：

```bash
docker compose --env-file .env.integration down --volumes
test "$(realpath /tmp/ticketwatch-integration-data)" = "/tmp/ticketwatch-integration-data"
rm -rf -- /tmp/ticketwatch-integration-data
```

只删除本次集成环境的命名卷，不操作 `/opt/ticketwatch` 或本机业务 SQLite。

- [ ] **Step 7: 提交必要修复或记录无变更**

如果步骤 1–6 暴露缺陷，先为缺陷增加回归测试，只提交该测试和最小修复。因为 Tasks 1–11 已经创建并跟踪所有目标文件，使用 `git add -u` 只暂存这些已跟踪修正：

```bash
git add -u
git commit -m "fix: pass private server integration checks"
```

如果没有缺陷，不创建空提交；在执行记录中保存全部命令和结果。

---

### Task 13: 在阿里云服务器迁移并完成真实链路验收

**Files:**
- Server state: `/opt/ticketwatch/`
- Evidence only: 命令退出码、去敏健康 JSON、邮件到达时间和备份恢复结果。

**Interfaces:**
- Consumes all Tasks 1–12 deliverables.
- Produces a running private server deployment reachable only through SSH tunnel.

- [ ] **Step 1: 确认部署授权和精确版本**

在执行任何服务器写操作前，与用户确认 SSH 访问方式和要部署的 Git commit SHA。记录当前服务器 Docker 状态、监听端口和 `/opt/ticketwatch` 是否已存在；若目录已有未知数据则停止，不覆盖。

- [ ] **Step 2: 按手册安装 Compose 和创建目录**

安装 Docker Compose plugin，创建固定目录与权限，将仓库检出到 `/opt/ticketwatch/app`，生成 `/opt/ticketwatch/.env`。运行 `docker compose config --quiet` 后才构建镜像。

- [ ] **Step 3: 部署数据库并导入 SQLite 数据**

上传 `core-data.json` 与本地 manifest；启动 PostgreSQL 和 Redis，执行 migration 和 `loaddata`，生成服务器 manifest。两个 manifest 必须逐字一致；不一致时保持 Worker 停止并调查具体模型摘要。

- [ ] **Step 4: 启动 Web 并验证私有边界**

启动 Web，通过 Mac SSH 隧道访问页面。服务器执行 `ss -lntp`，确认只有 `127.0.0.1:8000` 而非 `0.0.0.0:8000`；从外部访问 `47.116.69.108:8000` 必须失败。确认 5432 和 6379 未发布。

- [ ] **Step 5: 完成 Agent Mail 设备授权**

在 Worker 容器实际 Linux keyring 环境执行 OAuth 登录、`+me`、真实测试邮件、Worker 容器重启和再次 `+me`。五项均成功且主邮箱严格匹配 `mijiatong@agent.qq.com` 后，才把邮件配置标为 verified。若凭据不能跨重启，停止部署的邮件阶段，不写长期明文 token。

- [ ] **Step 6: 启动 Worker 并执行真实猫眼闭环**

选择猫眼当前确有排片的目标创建监控任务，记录任务 ID。执行一次“立即检查”，确认：`CheckRun=SUCCEEDED`、任务进入 `DETECTED/COMPLETED`、开票通知只有一条、邮件由 `mijiatong@agent.qq.com` 到达 `850634546@qq.com`。再次运行检查不得生成第二封开票邮件。

- [ ] **Step 7: 验证多任务和三级频率**

创建至少三条不同日期或影院任务，分别落入紧急、临近和远期区间。检查 `next_check_at` 与 60/300/900 秒默认值一致；修改设置后再次核对三个任务的重新调度，并验证 59 秒被拒绝。

- [ ] **Step 8: 验证重启、Redis 丢失与端口安全**

依次重启 Web、Worker、Redis 和整套 Compose，观察任务、通知、Worker 心跳和页面。Redis 重启后 PostgreSQL 数据数量与 manifest 不变。再次执行 `ss -lntp` 和阿里云安全组检查。

- [ ] **Step 9: 启用备份并实际恢复**

安装 systemd service/timer，手动运行一次 service，确认生成权限受限的压缩备份并更新 `/statusz`。对该文件运行 `verify-backup.sh`，确认临时数据库恢复成功且已在 trap 中删除。把一份备份通过 `scp` 下载到 Mac 并核对 SHA-256。

- [ ] **Step 10: 完成最终验收记录**

逐项记录规格第 14 节的 16 条验收结果。保存的信息只包括任务 UUID、状态、时间、数量、Git SHA、镜像 ID、备份 SHA-256 和去敏错误码；不保存 OAuth token、数据库密码、Cookie 或完整邮件正文。

Run final gate on server:

```bash
cd /opt/ticketwatch/app
docker compose --env-file /opt/ticketwatch/.env ps
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/statusz
systemctl status ticketwatch-backup.timer --no-pager
ss -lntp
```

Expected: 四服务运行、healthz 正常、statusz 无失败状态、备份 timer 已启用，8000 只绑定回环地址，5432/6379 未监听宿主机公网接口。
