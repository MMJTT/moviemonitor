# TicketWatch 猫眼本地开票监控

TicketWatch 是一个中文网页应用。它可以在本机运行，也可以作为仅通过 SSH 隧道访问的轻量私有服务器运行；它能同时监控多个不同的“城市 + 电影 + 日期 + 影院”目标，在目标影院首次出现有效的“选座购票”入口时发送一次开票邮件；日期结束仍未开票时发送一次到期邮件。

## 本地启动

需要 Python 3.12。首次运行：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runlocal
```

浏览器打开 <http://127.0.0.1:8000>。`runlocal` 只监听 `127.0.0.1:8000`，并在同一个进程中启动唯一的后台调度线程。关闭浏览器不会停止监控；在运行命令的终端按 `Ctrl-C` 或结束该进程才会停止网页和监控。再次运行同一命令后，应用会从 SQLite 中恢复任务、检查计划和待处理通知。

## 使用流程

1. 打开“邮件”，填写收件地址并发送测试邮件。测试成功前不能创建任务。
2. 打开“新建任务”，选择城市并粘贴猫眼影院列表的 HTTPS 链接。链接必须包含数字 `movieId` 和 `YYYY-MM-DD` 格式的 `showDate`。
3. 预览服务端验证后的电影、日期和影院列表，选择一家可见影院；如果影院尚未出现，也可以手动输入完整影院名称。
4. 创建任务。调度器会立即被唤醒检查，之后按设置中的固定间隔继续。
5. 在仪表盘或任务详情查看状态、最近 20 次检查和邮件通知状态。监控中可以立即检查、暂停或取消；暂停后可以恢复。

同一时间可以存在多个未结束任务，每个任务只监控一家影院。相同城市、电影、日期和影院的未结束任务不能重复创建，以避免重复请求和重复邮件。

## Agent Mail

应用固定使用 `mijiatong@agent.qq.com` 发件，不保存个人邮箱密码或 SMTP 授权码。首次运行前安装并授权 Agent Mail CLI：

```bash
npm install -g @tencent-qqmail/agently-cli
AGENTLY_WORKSPACE=codex agently-cli auth login
AGENTLY_WORKSPACE=codex agently-cli +me
```

程序固定使用 Agent Mail 的 `codex` workspace；上述 `+me` 命令显示的主邮箱必须是 `mijiatong@agent.qq.com`。OAuth 授权由 CLI 保存在 macOS 钥匙串中；TicketWatch 的 SQLite 只保存收件地址与验证状态。保存邮件设置时会先发送测试邮件，成功后才允许创建任务。授权失效时重新运行 `AGENTLY_WORKSPACE=codex agently-cli auth login`，再回到“邮件”页重新测试。

## 间隔、异常与恢复

“设置”页可以修改固定轮询间隔，硬性下限是 60 秒。保存后，正在监控任务的下次检查会按新间隔重新安排；暂停或异常任务不会被偷偷重新启用。

网络、限流、验证码或页面结构异常不会被当作开票。平台或结构异常连续达到五次后，任务进入 `ERROR` 并停止自动检查。请先用浏览器确认猫眼页面和链接仍正常，再在仪表盘或详情页点击“恢复监控”；恢复会清除连续失败计数并安排一次立即检查。也可以直接取消任务。

如果通知显示“发送中”，代表进程可能在 Agent Mail 已接收邮件后中断，发送结果不确定。应用不会自动重发这种通知，以避免重复邮件。

## 本地数据

- `db.sqlite3`：任务、检查计划、检查摘要和通知状态。

数据库已加入 Git 忽略规则。页面和日志不会保存或显示完整猫眼响应、完整邮件正文、Cookie 或 Agent Mail OAuth Token。

## 私有服务器部署

轻量服务器版使用 Docker Compose、PostgreSQL、Redis 和独立 Worker，Web 只绑定服务器 `127.0.0.1:8000`，由 SSH 隧道访问；PostgreSQL 和 Redis 不发布宿主机端口。首次 SQLite 迁移、Agent Mail Linux 授权、每日备份、升级和回滚请严格按[私有服务器部署与回滚手册](docs/server-deployment.md)执行。

## MVP 限制

- 这是无登录的单所有者工具。本机模式只应通过回环地址访问；服务器模式也只能通过 SSH 隧道访问，不提供公网部署、域名、HTTPS 或应用登录。
- 私有服务器版支持 Docker、PostgreSQL、Redis 和单一轻量 Worker；不支持多用户、邀请、权限、Celery、水平扩容或其他云端公开运维能力。
- 不处理验证码，不执行自动选座、下单或付款，也不解析票价、场次、影厅、座位或余票。
- 只支持猫眼影院列表 URL、一个城市、一个电影日期和一家影院；不支持大麦等其他平台。
- 开票邮件成功后任务完成，不继续监控新增场次或回流票。

## 开发检查

自动测试只使用固定、去敏的猫眼 HTML 和模拟 Agent Mail，不会访问真实猫眼或发送真实邮件：

```bash
.venv/bin/pytest -v
.venv/bin/ruff check . --exclude monitor.py
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
```
