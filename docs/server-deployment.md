# TicketWatch 私有服务器部署与回滚手册

本手册适用于 Alibaba Cloud Linux 4 的单所有者私有部署。Web 仅监听服务器回环地址，所有者通过 SSH 隧道访问。日常操作使用 `admin`；只有标明 `sudo` 的命令提升权限。

不要开放 `80`、`443`、`8000`、`5432` 或 `6379`。阿里云安全组只保留 `22/TCP`，并尽可能限制 SSH 来源 IP。所有 Git 版本均使用经过审阅的完整 SHA，绝不使用浮动分支或 `latest`。

## 部署前检查

先按阿里云 Linux 文档安装并启动 Docker Engine，再运行以下检查及 Compose 插件安装：

```bash
cat /etc/os-release
nproc
free -h
df -h /
docker --version
sudo dnf install -y docker-compose-plugin
docker compose version
```

确认 `admin` 已重新登录并可直接运行 `docker ps`。若 `/opt/ticketwatch` 已存在且含有未知数据，停止操作；不要覆盖它。

## 从本机导出 SQLite

先在运行 `runlocal` 的终端按 `Ctrl-C`，确认没有第二个 `runlocal` 进程；直到迁移验收完成前不要向本地应用写新数据。按固定顺序执行：先复制 SQLite 原件，再将本地 schema 迁移到含 `RuntimeState` 的 `0007_runtime_state`，然后导出业务数据。

```bash
mkdir -p migration-data
cp -p db.sqlite3 migration-data/db-before-server.sqlite3
.venv/bin/python manage.py migrate
.venv/bin/python manage.py data_manifest --output migration-data/local.manifest.json
.venv/bin/python manage.py dumpdata core.AppSetting core.AgentMailConfig core.MonitorTask core.CheckRun core.Notification --indent 2 --output migration-data/core-data.json
sha256sum migration-data/db-before-server.sqlite3 migration-data/local.manifest.json migration-data/core-data.json | tee migration-data/SHA256SUMS
```

记录三个文件的 SHA-256。绝不删除 SQLite 原件；至少保留到服务器验收及一次 PostgreSQL 恢复演练均成功后。

## 创建服务器目录和环境文件

备份脚本会拒绝不可信路径：`/opt`、`/opt/ticketwatch`、`data` 和 `data/backups` 必须为 `root` 或 `admin` 所有，且组/其他用户不可写。PostgreSQL 和 Redis 镜像使用 `999:999`；Worker 使用 `10001:10001`。`AGENTLY_DATA_DIR` 需在 Compose 之前创建为 `10001:10001`、`0700`，因为 Compose 对这个 bind mount 设置了 `create_host_path: false`。

```bash
sudo install -d -o root -g root -m 0755 /opt/ticketwatch
sudo install -d -o admin -g admin -m 0755 /opt/ticketwatch/app
sudo install -d -o root -g root -m 0755 /opt/ticketwatch/data
sudo install -d -o 999 -g 999 -m 0700 /opt/ticketwatch/data/postgres
sudo install -d -o 999 -g 999 -m 0700 /opt/ticketwatch/data/redis
sudo install -d -o 10001 -g 10001 -m 0700 /opt/ticketwatch/data/agently
sudo install -d -o admin -g admin -m 0700 /opt/ticketwatch/data/backups
sudo install -d -o admin -g admin -m 0700 /opt/ticketwatch/logs
sudo install -d -o admin -g admin -m 0751 /opt/ticketwatch/migration-data
```

检出明确提交。以下 SHA 是当前实现基线；若部署审阅过的更新版本，替换为它的完整 40 位 SHA。

```bash
REPOSITORY_URL='ssh://git@github.com/REPLACE-WITH-OWNER/ticketwatch.git'
RELEASE_SHA='49b4549308c056db07a44174983ac86e9d73552b'
sudo -u admin git clone "$REPOSITORY_URL" /opt/ticketwatch/app
sudo -u admin git -C /opt/ticketwatch/app fetch --tags origin
sudo -u admin git -C /opt/ticketwatch/app checkout --detach "$RELEASE_SHA"
test "$(sudo -u admin git -C /opt/ticketwatch/app rev-parse HEAD)" = "$RELEASE_SHA"
```

创建 `.env` 时不得打印或提交秘密。文件必须是正规文件（非符号链接），由 `admin` 或 `root` 所有，权限 `0600`。`POSTGRES_USER` 和 `POSTGRES_DB` 使用备份脚本接受的简单小写标识符：`[a-z_][a-z0-9_]{0,62}`。

```bash
cd /opt/ticketwatch/app
set +x
umask 077
ENV_TMP=$(mktemp /tmp/ticketwatch-env.XXXXXX)
trap 'rm -f -- "$ENV_TMP"' EXIT
cp .env.example "$ENV_TMP"
sed -i \
  -e 's|^TICKETWATCH_ENV=.*|TICKETWATCH_ENV=production|' \
  -e 's|^DJANGO_DEBUG=.*|DJANGO_DEBUG=false|' \
  -e 's|^TICKETWATCH_ENV_FILE=.*|TICKETWATCH_ENV_FILE=/opt/ticketwatch/.env|' \
  -e 's|^POSTGRES_DATA_DIR=.*|POSTGRES_DATA_DIR=/opt/ticketwatch/data/postgres|' \
  -e 's|^REDIS_DATA_DIR=.*|REDIS_DATA_DIR=/opt/ticketwatch/data/redis|' \
  -e 's|^AGENTLY_DATA_DIR=.*|AGENTLY_DATA_DIR=/opt/ticketwatch/data/agently|' \
  -e 's|^AGENTLY_WORKSPACE=.*|AGENTLY_WORKSPACE=ticketwatch-server|' \
  -e 's|^POSTGRES_USER=.*|POSTGRES_USER=ticketwatch|' \
  -e 's|^POSTGRES_DB=.*|POSTGRES_DB=ticketwatch|' "$ENV_TMP"
sed -i \
  -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$(openssl rand -hex 32)|" \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 32)|" \
  -e "s|^AGENTLY_KEYRING_PASSWORD=.*|AGENTLY_KEYRING_PASSWORD=$(openssl rand -hex 32)|" "$ENV_TMP"
sudo install -o admin -g admin -m 0600 "$ENV_TMP" /opt/ticketwatch/.env
rm -f -- "$ENV_TMP"
trap - EXIT
sudo test -f /opt/ticketwatch/.env ! -L /opt/ticketwatch/.env
sudo test "$(stat -c '%U:%a' /opt/ticketwatch/.env)" = 'admin:600'
```

若 policy 要求 `root` 所有，可改为 `root:600`；不要在交互 shell 中 `source` 此文件。首次 Compose 前进行最后的路径检查：

```bash
sudo namei -l /opt/ticketwatch/.env
sudo stat -c '%n %U:%G %a' /opt /opt/ticketwatch /opt/ticketwatch/data /opt/ticketwatch/data/backups /opt/ticketwatch/.env
sudo stat -c '%n %u:%g %a' /opt/ticketwatch/data/postgres /opt/ticketwatch/data/redis /opt/ticketwatch/data/agently
test "$(sudo stat -c '%u:%g:%a' /opt/ticketwatch/data/agently)" = '10001:10001:700'
docker compose --env-file /opt/ticketwatch/.env config --quiet
```

## 上传、导入和启动

在 Mac 上传明确的临时迁移材料，并在服务器核对上传的两个业务文件哈希。`SHA256SUMS` 里 SQLite 那一行只在 Mac 本地保留，无需上传原 SQLite。

```bash
scp migration-data/core-data.json migration-data/local.manifest.json migration-data/SHA256SUMS admin@47.116.69.108:/opt/ticketwatch/migration-data/
ssh admin@47.116.69.108 'cd /opt/ticketwatch/migration-data && grep -E " (core-data\.json|local\.manifest\.json)\$" SHA256SUMS | sha256sum -c -'
sudo chown 10001:10001 /opt/ticketwatch/migration-data/core-data.json /opt/ticketwatch/migration-data/local.manifest.json
sudo chmod 0600 /opt/ticketwatch/migration-data/core-data.json /opt/ticketwatch/migration-data/local.manifest.json
```

严格按此顺序启动。`loaddata` 从只读临时 bind mount 读取；在 manifest 逐字一致前不要启动 Worker。

```bash
cd /opt/ticketwatch/app
docker compose --env-file /opt/ticketwatch/.env build web worker
docker compose --env-file /opt/ticketwatch/.env up -d postgres redis
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --volume /opt/ticketwatch/migration-data:/migration:ro web python manage.py loaddata /migration/core-data.json
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web sh -ec 'python manage.py data_manifest --output /tmp/server.manifest.json >/dev/null && cat /tmp/server.manifest.json' > /opt/ticketwatch/migration-data/server.manifest.json
cmp -s /opt/ticketwatch/migration-data/local.manifest.json /opt/ticketwatch/migration-data/server.manifest.json
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web python manage.py showmigrations core
docker compose --env-file /opt/ticketwatch/.env up -d web
curl --fail http://127.0.0.1:8000/healthz
docker compose --env-file /opt/ticketwatch/.env run --rm worker agently-cli auth login
docker compose --env-file /opt/ticketwatch/.env run --rm worker python manage.py agent_mail_preflight --send-test
docker compose --env-file /opt/ticketwatch/.env up -d worker
docker compose --env-file /opt/ticketwatch/.env ps
curl --fail http://127.0.0.1:8000/statusz
ss -lntp
```

若 `cmp` 失败，保留两个 manifest、停止 Worker 并调查各业务模型摘要；不要向已填充的数据库再跑 `loaddata`。`showmigrations` 必须显示 `[X] 0007_runtime_state`。Agent Mail 登录是 Worker Linux/keyring 中的交互设备授权；不得把长期明文访问令牌写入 `.env`。预检会发真实测试邮件。授权后确认凭据跨重启保存：

```bash
docker compose --env-file /opt/ticketwatch/.env run --rm worker agently-cli +me
docker compose --env-file /opt/ticketwatch/.env restart worker
docker compose --env-file /opt/ticketwatch/.env run --rm worker agently-cli +me
```

若身份不正确或凭据不能跨重启，保持 Worker 停止并回到设计评审。

## 通过 SSH 隧道访问

在 Mac 运行并保持这个进程：

```bash
ssh -N -L 18000:127.0.0.1:8000 admin@47.116.69.108
```

浏览器只访问 <http://127.0.0.1:18000>。SSH 中断只影响网页通道，不会停止 Worker、Web、PostgreSQL 或 Redis。服务器的 `ss -lntp` 必须显示 Web 为 `127.0.0.1:8000`，且 PostgreSQL/Redis 没有宿主机端口。

## 每日备份和恢复演练

备份脚本 fail closed：它检查可信路径、符号链接、权限、环境中的 PostgreSQL 标识符及临时输出。脚本以 `flock` 锁住备份目录；并发手动任务和 timer 任务会串行执行。它是非交互式的：`admin` 必须已拥有 Docker 使用权，且 `--no-password` 认证失败即失败，不能等待密码输入。

安装 timer，手动备份一次，再只恢复到随机临时数据库进行演练。`verify-backup.sh` 的 trap 会删除该临时数据库，不会删除生产数据库。

```bash
sudo install -o root -g root -m 0644 /opt/ticketwatch/app/deploy/ticketwatch-backup.service /etc/systemd/system/ticketwatch-backup.service
sudo install -o root -g root -m 0644 /opt/ticketwatch/app/deploy/ticketwatch-backup.timer /etc/systemd/system/ticketwatch-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now ticketwatch-backup.timer
sudo systemctl start ticketwatch-backup.service
sudo systemctl status ticketwatch-backup.timer --no-pager
LATEST_BACKUP=$(find /opt/ticketwatch/data/backups -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-)
test -n "$LATEST_BACKUP"
sudo -u admin /opt/ticketwatch/app/deploy/verify-backup.sh "$LATEST_BACKUP"
sha256sum "$LATEST_BACKUP"
```

Timer 每日 03:20 Asia/Shanghai 执行（最多随机延迟五分钟），成功备份保留七天。每周至少复制一个已演练备份到 Mac 并比较 SHA-256；普通 PostgreSQL 备份不含 `.env` 或 Agent Mail 凭据。

## 升级

升级前保留当前 SHA、当前两个容器的 image reference 和升级前备份。停掉 Web/Worker 防止旧代码在迁移中写入，检出精确新 SHA、构建、迁移、启动 Web、预检，再启动 Worker。

```bash
cd /opt/ticketwatch/app
PREVIOUS_SHA=$(git rev-parse HEAD)
WEB_CONTAINER=$(docker compose --env-file /opt/ticketwatch/.env ps -q web)
WORKER_CONTAINER=$(docker compose --env-file /opt/ticketwatch/.env ps -q worker)
WEB_IMAGE_REF=$(docker inspect --format '{{.Config.Image}}' "$WEB_CONTAINER")
WORKER_IMAGE_REF=$(docker inspect --format '{{.Config.Image}}' "$WORKER_CONTAINER")
docker image tag "$(docker inspect --format '{{.Image}}' "$WEB_CONTAINER")" "ticketwatch-rollback-web:$PREVIOUS_SHA"
docker image tag "$(docker inspect --format '{{.Image}}' "$WORKER_CONTAINER")" "ticketwatch-rollback-worker:$PREVIOUS_SHA"
./deploy/backup-postgres.sh
docker compose --env-file /opt/ticketwatch/.env stop worker web
NEW_SHA='REPLACE_WITH_AUDITED_40_CHARACTER_COMMIT_SHA'
git fetch --tags origin
git checkout --detach "$NEW_SHA"
test "$(git rev-parse HEAD)" = "$NEW_SHA"
docker compose --env-file /opt/ticketwatch/.env build web worker
docker compose --env-file /opt/ticketwatch/.env up -d postgres redis
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env up -d --no-deps web
curl --fail http://127.0.0.1:8000/healthz
docker compose --env-file /opt/ticketwatch/.env run --rm worker python manage.py agent_mail_preflight
docker compose --env-file /opt/ticketwatch/.env up -d --no-deps worker
curl --fail http://127.0.0.1:8000/statusz
```

仅部署前后兼容的 schema 迁移；检查任务调度、Worker 心跳、私有状态页和 Agent Mail 身份。

## 回滚

先区分应用故障与已确认的数据损坏。应用问题通常只切回上一 SHA 和保留的 image；不要因为一次应用回滚而恢复 PostgreSQL。

```bash
cd /opt/ticketwatch/app
docker compose --env-file /opt/ticketwatch/.env stop worker web
git checkout --detach "$PREVIOUS_SHA"
test "$(git rev-parse HEAD)" = "$PREVIOUS_SHA"
docker image tag "ticketwatch-rollback-web:$PREVIOUS_SHA" "$WEB_IMAGE_REF"
docker image tag "ticketwatch-rollback-worker:$PREVIOUS_SHA" "$WORKER_IMAGE_REF"
docker compose --env-file /opt/ticketwatch/.env up -d --no-build --no-deps web
curl --fail http://127.0.0.1:8000/healthz
docker compose --env-file /opt/ticketwatch/.env run --rm worker python manage.py agent_mail_preflight
docker compose --env-file /opt/ticketwatch/.env up -d --no-build --no-deps worker
curl --fail http://127.0.0.1:8000/statusz
```

若回滚 image 被清理，只能在检出 `PREVIOUS_SHA` 后重新构建，并记录这一例外。

只有确认数据已损坏、指定备份已通过 `verify-backup.sh`，并且先完成故障现场备份后，才可运行下面这个显式破坏性步骤。它会删除并重建名为 `POSTGRES_DB` 的生产数据库；不要把它当作普通应用回滚。

```bash
BACKUP='/opt/ticketwatch/data/backups/ticketwatch-YYYYMMDDTHHMMSSZ.sql.gz'
test -f "$BACKUP"
./deploy/verify-backup.sh "$BACKUP"
./deploy/backup-postgres.sh
docker compose --env-file /opt/ticketwatch/.env stop worker web
gzip -dc -- "$BACKUP" | docker compose --env-file /opt/ticketwatch/.env exec -T postgres sh -ec 'dropdb --if-exists --no-password --username "$POSTGRES_USER" "$POSTGRES_DB"; createdb --no-password --username "$POSTGRES_USER" "$POSTGRES_DB"; pg_restore --exit-on-error --no-owner --no-acl --no-password --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"'
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env up -d --no-build --no-deps web worker
curl --fail http://127.0.0.1:8000/statusz
```

恢复后核对业务表数量、活动任务及 `next_check_at`、通知唯一性、`0007_runtime_state` 和私有 UI。保留故障现场备份、恢复记录和本地 SQLite 原件。迁移 JSON/manifests 是明确临时目标；仅在验收后，以指定文件的方式清理，绝不对广泛路径执行删除。
