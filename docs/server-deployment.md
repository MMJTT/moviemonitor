# TicketWatch 私有服务器部署与回滚手册

本手册适用于 Alibaba Cloud Linux 4 的单所有者私有部署。Web 仅监听服务器回环地址，所有者通过 SSH 隧道访问。日常操作使用 `admin`；只有标明 `sudo` 的命令提升权限。除非代码块明确标注“Mac”，所有 Bash 代码块都在服务器的 `admin` 会话中执行。

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

先在运行 `runlocal` 的终端按 `Ctrl-C`，确认没有第二个 `runlocal` 进程；直到迁移验收完成前不要向本地应用写新数据。按固定顺序执行：先复制 SQLite 原件，再将本地 schema 迁移到含持久化 Worker 邮件复验退避的 `core.0009_agent_mail_verification_retry`，然后导出业务数据。

在 **Mac 本机仓库** 执行：

```bash
mkdir -p migration-data
cp -p db.sqlite3 migration-data/db-before-server.sqlite3
.venv/bin/python manage.py migrate
.venv/bin/python manage.py data_manifest --output migration-data/local.manifest.json
.venv/bin/python manage.py dumpdata core.AppSetting core.AgentMailConfig core.MonitorTask core.CheckRun core.Notification --indent 2 --output migration-data/core-data.json
(cd migration-data && shasum -a 256 db-before-server.sqlite3 local.manifest.json core-data.json | tee SHA256SUMS)
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
sudo install -d -o admin -g admin -m 0700 /opt/ticketwatch/data/incidents
sudo install -d -o admin -g admin -m 0700 /opt/ticketwatch/data/upgrade-records
sudo install -d -o admin -g admin -m 0700 /opt/ticketwatch/logs
sudo install -d -o admin -g admin -m 0751 /opt/ticketwatch/migration-data
```

发布版本不能硬编码在本手册中。每次发布由审阅者提供外部 `release-manifest.env`，其中的 `RELEASE_SHA` 必须是已审阅并发布到 `RELEASE_REF` 的完整 40 位 SHA。服务器已知无法稳定访问 GitHub 或 Docker Hub 时，默认使用下面的离线 Git bundle 与四镜像归档；不要在服务器上反复尝试在线拉取。

### 默认：制作并传输离线发布包

在 **Mac 本机仓库** 执行。把 `REPLACE_WITH_AUDITED_40_CHARACTER_COMMIT_SHA` 替换为本次独立审阅通过的 SHA；构建上下文来自该提交的 `git archive`，不是当前未提交工作区。四个镜像都必须是 `linux/amd64`。

```bash
set -Eeuo pipefail
REPOSITORY_URL='https://github.com/MMJTT/moviemonitor.git'
RELEASE_REF='refs/heads/feature/ticket-monitor'
RELEASE_SHA='REPLACE_WITH_AUDITED_40_CHARACTER_COMMIT_SHA'
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$'
test "$(git ls-remote --exit-code "$REPOSITORY_URL" "$RELEASE_REF" | awk '{print $1}')" = "$RELEASE_SHA"
git fetch --no-tags "$REPOSITORY_URL" "$RELEASE_REF"
test "$(git rev-parse FETCH_HEAD)" = "$RELEASE_SHA"
git merge-base --is-ancestor "$RELEASE_SHA" FETCH_HEAD
test "$(git rev-parse "$RELEASE_REF")" = "$RELEASE_SHA"
RELEASE_STAGE=$(mktemp -d /tmp/ticketwatch-release.XXXXXX)
RELEASE_SOURCE=$(mktemp -d /tmp/ticketwatch-source.XXXXXX)
cleanup_release() { rm -rf -- "$RELEASE_STAGE" "$RELEASE_SOURCE"; }
trap cleanup_release EXIT
git bundle create "$RELEASE_STAGE/ticketwatch-release.bundle" "$RELEASE_REF"
git bundle verify "$RELEASE_STAGE/ticketwatch-release.bundle"
git bundle list-heads "$RELEASE_STAGE/ticketwatch-release.bundle" "$RELEASE_REF" | grep -qx "$RELEASE_SHA $RELEASE_REF"
git archive "$RELEASE_SHA" | tar -x -C "$RELEASE_SOURCE"
docker buildx build --platform linux/amd64 --target web --tag "ticketwatch-release-web:$RELEASE_SHA" --load "$RELEASE_SOURCE"
docker buildx build --platform linux/amd64 --target worker --tag "ticketwatch-release-worker:$RELEASE_SHA" --load "$RELEASE_SOURCE"
docker pull --platform linux/amd64 postgres:16-bookworm
docker pull --platform linux/amd64 redis:7-alpine
WEB_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "ticketwatch-release-web:$RELEASE_SHA")
WORKER_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "ticketwatch-release-worker:$RELEASE_SHA")
POSTGRES_IMAGE_ID=$(docker image inspect --format '{{.Id}}' postgres:16-bookworm)
REDIS_IMAGE_ID=$(docker image inspect --format '{{.Id}}' redis:7-alpine)
for image in "ticketwatch-release-web:$RELEASE_SHA" "ticketwatch-release-worker:$RELEASE_SHA" postgres:16-bookworm redis:7-alpine; do
  test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" = 'linux/amd64'
done
docker save "ticketwatch-release-web:$RELEASE_SHA" "ticketwatch-release-worker:$RELEASE_SHA" postgres:16-bookworm redis:7-alpine | gzip -n > "$RELEASE_STAGE/ticketwatch-images.tar.gz"
{
  printf 'RELEASE_SHA=%s\nRELEASE_REF=%s\n' "$RELEASE_SHA" "$RELEASE_REF"
  printf 'WEB_IMAGE=ticketwatch-release-web:%s\nWEB_IMAGE_ID=%s\n' "$RELEASE_SHA" "$WEB_IMAGE_ID"
  printf 'WORKER_IMAGE=ticketwatch-release-worker:%s\nWORKER_IMAGE_ID=%s\n' "$RELEASE_SHA" "$WORKER_IMAGE_ID"
  printf 'POSTGRES_IMAGE=postgres:16-bookworm\nPOSTGRES_IMAGE_ID=%s\n' "$POSTGRES_IMAGE_ID"
  printf 'REDIS_IMAGE=redis:7-alpine\nREDIS_IMAGE_ID=%s\n' "$REDIS_IMAGE_ID"
} > "$RELEASE_STAGE/release-manifest.env"
(cd "$RELEASE_STAGE" && shasum -a 256 ticketwatch-release.bundle ticketwatch-images.tar.gz release-manifest.env > release-SHA256SUMS)
(cd "$RELEASE_STAGE" && shasum -a 256 -c release-SHA256SUMS)
scp "$RELEASE_STAGE/ticketwatch-release.bundle" "$RELEASE_STAGE/ticketwatch-images.tar.gz" "$RELEASE_STAGE/release-manifest.env" "$RELEASE_STAGE/release-SHA256SUMS" admin@47.116.69.108:/opt/ticketwatch/migration-data/
```

在 **服务器** 校验传输结果。`release-manifest.env` 不含秘密，但仍必须是 `admin` 的普通只读文件。完整性、bundle ref、提交可达性、加载后的 image ID 和架构任一不匹配即停止。已有部署只验证并暂存这些材料，不在本步骤切换运行版本；切换由后面的受保护升级流程完成。

```bash
set -Eeuo pipefail
cd /opt/ticketwatch/migration-data
for file in ticketwatch-release.bundle ticketwatch-images.tar.gz release-manifest.env release-SHA256SUMS; do
  test -f "$file" && test ! -L "$file"
done
chmod 0600 ticketwatch-release.bundle ticketwatch-images.tar.gz release-manifest.env release-SHA256SUMS
sha256sum -c release-SHA256SUMS
RELEASE_MANIFEST=/opt/ticketwatch/migration-data/release-manifest.env
for key in RELEASE_SHA RELEASE_REF WEB_IMAGE WEB_IMAGE_ID WORKER_IMAGE WORKER_IMAGE_ID POSTGRES_IMAGE POSTGRES_IMAGE_ID REDIS_IMAGE REDIS_IMAGE_ID; do
  test "$(grep -c "^$key=" "$RELEASE_MANIFEST")" = 1
done
release_value() { sed -n "s/^$1=//p" "$RELEASE_MANIFEST"; }
RELEASE_SHA=$(release_value RELEASE_SHA)
RELEASE_REF=$(release_value RELEASE_REF)
printf '%s\n' "$RELEASE_SHA" | grep -Eq '^[0-9a-f]{40}$'
printf '%s\n' "$RELEASE_REF" | grep -Eq '^refs/heads/[A-Za-z0-9._/-]+$'
git bundle list-heads ticketwatch-release.bundle "$RELEASE_REF" | grep -qx "$RELEASE_SHA $RELEASE_REF"
if [[ ! -d /opt/ticketwatch/app/.git ]]; then
  sudo -u admin git -C /opt/ticketwatch/app init
fi
sudo -u admin git -C /opt/ticketwatch/app bundle verify /opt/ticketwatch/migration-data/ticketwatch-release.bundle
gzip -dc -- ticketwatch-images.tar.gz | docker load >/dev/null
for prefix in WEB WORKER POSTGRES REDIS; do
  IMAGE=$(release_value "${prefix}_IMAGE")
  EXPECTED_ID=$(release_value "${prefix}_IMAGE_ID")
  test "$(docker image inspect --format '{{.Id}}' "$IMAGE")" = "$EXPECTED_ID"
  test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE")" = 'linux/amd64'
  docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$IMAGE"
done
if [[ -z "$(sudo -u admin git -C /opt/ticketwatch/app rev-parse --verify HEAD 2>/dev/null || true)" ]]; then
  sudo -u admin git -C /opt/ticketwatch/app fetch /opt/ticketwatch/migration-data/ticketwatch-release.bundle "$RELEASE_REF"
  test "$(sudo -u admin git -C /opt/ticketwatch/app rev-parse FETCH_HEAD)" = "$RELEASE_SHA"
  sudo -u admin git -C /opt/ticketwatch/app merge-base --is-ancestor "$RELEASE_SHA" FETCH_HEAD
  sudo -u admin git -C /opt/ticketwatch/app checkout --detach "$RELEASE_SHA"
  test "$(sudo -u admin git -C /opt/ticketwatch/app rev-parse HEAD)" = "$RELEASE_SHA"
fi
```

### 可选：服务器在线取代码和构建

只有服务器对 GitHub、Docker Hub 和 npm 的出口已实际验证稳定时才使用在线路径。仍从外部 release manifest 读取 `RELEASE_SHA`，并验证远端 ref 精确指向它；在线构建不能退回浮动分支或 `latest`。

```bash
set -Eeuo pipefail
REPOSITORY_URL='https://github.com/MMJTT/moviemonitor.git'
RELEASE_REF='refs/heads/feature/ticket-monitor'
RELEASE_MANIFEST=/opt/ticketwatch/migration-data/release-manifest.env
RELEASE_SHA=$(sed -n 's/^RELEASE_SHA=//p' "$RELEASE_MANIFEST")
test "$(git ls-remote --exit-code "$REPOSITORY_URL" "$RELEASE_REF" | awk '{print $1}')" = "$RELEASE_SHA"
if [[ ! -d /opt/ticketwatch/app/.git ]]; then
  sudo -u admin git -C /opt/ticketwatch/app init
fi
if ! sudo -u admin git -C /opt/ticketwatch/app remote get-url origin >/dev/null 2>&1; then
  sudo -u admin git -C /opt/ticketwatch/app remote add origin "$REPOSITORY_URL"
fi
test "$(sudo -u admin git -C /opt/ticketwatch/app remote get-url origin)" = "$REPOSITORY_URL"
sudo -u admin git -C /opt/ticketwatch/app fetch --no-tags origin "$RELEASE_REF"
sudo -u admin git -C /opt/ticketwatch/app rev-parse --verify "$RELEASE_SHA^{commit}"
sudo -u admin git -C /opt/ticketwatch/app merge-base --is-ancestor "$RELEASE_SHA" FETCH_HEAD
sudo -u admin git -C /opt/ticketwatch/app checkout --detach "$RELEASE_SHA"
test "$(sudo -u admin git -C /opt/ticketwatch/app rev-parse HEAD)" = "$RELEASE_SHA"
docker build --platform linux/amd64 --target web --tag ticketwatch-web /opt/ticketwatch/app
docker build --platform linux/amd64 --target worker --tag ticketwatch-worker /opt/ticketwatch/app
docker pull --platform linux/amd64 postgres:16-bookworm
docker pull --platform linux/amd64 redis:7-alpine
test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' ticketwatch-web)" = 'linux/amd64'
test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' ticketwatch-worker)" = 'linux/amd64'
```

创建 `.env` 时不得打印或提交秘密。文件必须是正规文件（非符号链接），固定由 `admin:admin` 所有、权限 `0600`；Compose 和备份 timer 都以 `admin` 读取它。`POSTGRES_USER` 和 `POSTGRES_DB` 使用备份脚本接受的简单小写标识符：`[a-z_][a-z0-9_]{0,62}`。

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
sudo test -f /opt/ticketwatch/.env && sudo test ! -L /opt/ticketwatch/.env
sudo test "$(stat -c '%U:%a' /opt/ticketwatch/.env)" = 'admin:600'
```

不要在交互 shell 中 `source` 此文件。首次 Compose 前进行最后的路径检查：

```bash
sudo namei -l /opt/ticketwatch/.env
sudo stat -c '%n %U:%G %a' /opt /opt/ticketwatch /opt/ticketwatch/data /opt/ticketwatch/data/backups /opt/ticketwatch/.env
sudo stat -c '%n %u:%g %a' /opt/ticketwatch/data/postgres /opt/ticketwatch/data/redis /opt/ticketwatch/data/agently
test "$(sudo stat -c '%u:%g:%a' /opt/ticketwatch/data/agently)" = '10001:10001:700'
docker compose --env-file /opt/ticketwatch/.env config --quiet
```

## 上传、导入和启动

在 Mac 上传明确的临时迁移材料，并在服务器核对上传的两个业务文件哈希。`SHA256SUMS` 中的文件名是本地 basename，因而可被服务器的 GNU `sha256sum -c` 直接验证；SQLite 原件本身不上传。

在 **Mac** 执行：

```bash
scp migration-data/core-data.json migration-data/local.manifest.json migration-data/SHA256SUMS admin@47.116.69.108:/opt/ticketwatch/migration-data/
```

在 **服务器** 执行。目录保持 `admin` 所有且可供容器 traverse；清单和校验文件仍只由 `admin` 读取。只有导入 JSON 可由容器 UID `10001` 读取，且只读 bind mount 保持不变。

```bash
cd /opt/ticketwatch/migration-data
grep -E ' (core-data\.json|local\.manifest\.json)$' SHA256SUMS | sha256sum -c -
sudo chown 10001:admin core-data.json
sudo chmod 0640 core-data.json
sudo chown admin:admin local.manifest.json SHA256SUMS
sudo chmod 0600 local.manifest.json SHA256SUMS
```

严格按此顺序启动。离线发布先把已核验的 release 镜像标记为 Compose 的固定应用镜像名；`.env` 中的 `COMPOSE_PROJECT_NAME=ticketwatch` 保证这些名字不随检出目录变化。`loaddata` 通过标准输入从只读临时 bind mount 读取，避免在 `0751` 暂存目录中枚举文件；在 manifest 逐字一致前不要启动 Worker。Web 不挂载 `/opt/ticketwatch/data/agently` 或 `/home/ticketwatch`，也不接收任何 `AGENTLY_*` 环境变量；Web 容器绝不运行 `agently-cli`。只有 Worker 挂载 Linux keyring 和 Agent Mail 数据目录，并接收 `AGENTLY_WORKSPACE` 与 keyring 密码。

```bash
cd /opt/ticketwatch/app
RELEASE_MANIFEST=/opt/ticketwatch/migration-data/release-manifest.env
release_value() { sed -n "s/^$1=//p" "$RELEASE_MANIFEST"; }
if test -f /opt/ticketwatch/migration-data/ticketwatch-images.tar.gz; then
  WEB_RELEASE_IMAGE=$(release_value WEB_IMAGE)
  WORKER_RELEASE_IMAGE=$(release_value WORKER_IMAGE)
  docker image tag "$WEB_RELEASE_IMAGE" ticketwatch-web
  docker image tag "$WORKER_RELEASE_IMAGE" ticketwatch-worker
fi
docker compose --env-file /opt/ticketwatch/.env config --images | grep -qx ticketwatch-web
docker compose --env-file /opt/ticketwatch/.env config --images | grep -qx ticketwatch-worker
docker image inspect ticketwatch-web ticketwatch-worker >/dev/null
docker compose --env-file /opt/ticketwatch/.env up -d --wait postgres redis --pull never --no-build
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build --volume /opt/ticketwatch/migration-data:/migration:ro web sh -ec 'python manage.py loaddata --format=json - < /migration/core-data.json'
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build web sh -ec 'python manage.py data_manifest --output /tmp/server.manifest.json >/dev/null && cat /tmp/server.manifest.json' > /opt/ticketwatch/migration-data/server.manifest.json
if ! cmp -s /opt/ticketwatch/migration-data/local.manifest.json /opt/ticketwatch/migration-data/server.manifest.json; then
  echo 'manifest mismatch; preserve both files and keep Worker stopped' >&2
  exit 1
fi
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build web python manage.py showmigrations core
docker compose --env-file /opt/ticketwatch/.env up -d --pull never --no-build web
curl --fail http://127.0.0.1:8000/healthz
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker agently-cli auth login
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker python manage.py agent_mail_preflight --send-test
docker compose --env-file /opt/ticketwatch/.env up -d --pull never --no-build worker
docker compose --env-file /opt/ticketwatch/.env ps
curl --fail http://127.0.0.1:8000/statusz
ss -lntp
```

若 `cmp` 失败，保留两个 manifest、停止 Worker 并调查各业务模型摘要；不要向已填充的数据库再跑 `loaddata`。`showmigrations` 必须显示 `[X] 0009_agent_mail_verification_retry`。Agent Mail 登录是 Worker Linux/keyring 中的交互设备授权；不得把长期明文访问令牌写入 `.env`。预检会发真实测试邮件。授权后确认凭据跨重启保存：

```bash
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker agently-cli +me
docker compose --env-file /opt/ticketwatch/.env restart worker
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker agently-cli +me
```

若身份不正确或凭据不能跨重启，保持 Worker 停止并回到设计评审。

Worker 启动时先执行一次身份验证并写入有时效的验证证明，随后继续按六小时周期复验；Web 只读取 PostgreSQL 中的新鲜证明和 Worker 心跳。邮件设置页的验证按钮只把 Worker 请求持久化到 PostgreSQL，Redis 唤醒只是加速手段，失败时请求仍由 Worker 后续扫描领取。邮件验证失败不会停止电影检查，但新任务会在证明失效或 Worker 心跳过期时失败关闭；发送每封通知前仍由 Worker 实时确认身份。

最终验收不要用 Django shell 或直接写表绕过业务校验。必须通过生产 Web 页面和服务路径创建最终验收任务，并依次确认：Web 镜像中 `command -v agently-cli` 返回非零；邮件设置页的后台请求由 Worker 完成；新鲜证明和健康心跳允许创建任务；证明过期会留下一个持久化的重新验证请求；任务随后由 Worker 检查并按现有去重规则通知。

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

Timer 每日 03:20 Asia/Shanghai 执行（最多随机延迟五分钟），成功备份保留七天。普通 PostgreSQL 备份不含 `.env` 或 Agent Mail 凭据。

每周在 **Mac** 下载一个已演练备份；先取服务器源文件的 SHA-256，再对下载目标使用 macOS `shasum`，两者必须相同：

```bash
REMOTE_BACKUP='/opt/ticketwatch/data/backups/ticketwatch-YYYYMMDDTHHMMSSZ.sql.gz'
mkdir -p ticketwatch-backups
REMOTE_SHA=$(ssh admin@47.116.69.108 "sha256sum -- '$REMOTE_BACKUP' | awk '{print \$1}'")
scp "admin@47.116.69.108:$REMOTE_BACKUP" "ticketwatch-backups/$(basename "$REMOTE_BACKUP")"
LOCAL_BACKUP="ticketwatch-backups/$(basename "$REMOTE_BACKUP")"
LOCAL_SHA=$(shasum -a 256 "$LOCAL_BACKUP" | awk '{print $1}')
test "$REMOTE_SHA" = "$LOCAL_SHA"
```

## 升级

升级也默认使用已传输并完成两端哈希校验的离线 bundle、四镜像归档和外部 release manifest。升级前再次验证 bundle ref、提交和加载后镜像 ID/架构；任一检查失败时不能停止现有服务。随后在受保护的 `upgrade-records` 中持久记录当前/目标 SHA、不可变旧 image ID、原 image reference、升级前备份路径及校验和。停掉 Web/Worker 后只从已验证 bundle 检出目标提交，并以 `--pull never --no-build` 启动，避免切换过程中意外访问网络或构建另一份代码。

```bash
set -Eeuo pipefail
cd /opt/ticketwatch/migration-data
sha256sum -c release-SHA256SUMS
RELEASE_MANIFEST=/opt/ticketwatch/migration-data/release-manifest.env
release_value() { sed -n "s/^$1=//p" "$RELEASE_MANIFEST"; }
NEW_SHA=$(release_value RELEASE_SHA)
RELEASE_REF=$(release_value RELEASE_REF)
printf '%s\n' "$NEW_SHA" | grep -Eq '^[0-9a-f]{40}$'
git bundle list-heads ticketwatch-release.bundle "$RELEASE_REF" | grep -qx "$NEW_SHA $RELEASE_REF"
sudo -u admin git -C /opt/ticketwatch/app bundle verify /opt/ticketwatch/migration-data/ticketwatch-release.bundle
gzip -dc -- ticketwatch-images.tar.gz | docker load >/dev/null
for prefix in WEB WORKER POSTGRES REDIS; do
  IMAGE=$(release_value "${prefix}_IMAGE")
  EXPECTED_ID=$(release_value "${prefix}_IMAGE_ID")
  test "$(docker image inspect --format '{{.Id}}' "$IMAGE")" = "$EXPECTED_ID"
  test "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE")" = 'linux/amd64'
done
NEW_WEB_IMAGE=$(release_value WEB_IMAGE)
NEW_WORKER_IMAGE=$(release_value WORKER_IMAGE)

cd /opt/ticketwatch/app
PREVIOUS_SHA=$(git rev-parse HEAD)
WEB_CONTAINER=$(docker compose --env-file /opt/ticketwatch/.env ps -q web)
WORKER_CONTAINER=$(docker compose --env-file /opt/ticketwatch/.env ps -q worker)
test -n "$WEB_CONTAINER" && test -n "$WORKER_CONTAINER"
WEB_IMAGE_REF=$(docker inspect --format '{{.Config.Image}}' "$WEB_CONTAINER")
WORKER_IMAGE_REF=$(docker inspect --format '{{.Config.Image}}' "$WORKER_CONTAINER")
WEB_IMAGE_ID=$(docker inspect --format '{{.Image}}' "$WEB_CONTAINER")
WORKER_IMAGE_ID=$(docker inspect --format '{{.Image}}' "$WORKER_CONTAINER")
docker image tag "$WEB_IMAGE_ID" "ticketwatch-rollback-web:$PREVIOUS_SHA"
docker image tag "$WORKER_IMAGE_ID" "ticketwatch-rollback-worker:$PREVIOUS_SHA"
./deploy/backup-postgres.sh
UPGRADE_BACKUP=$(find /opt/ticketwatch/data/backups -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-)
test -n "$UPGRADE_BACKUP"
UPGRADE_BACKUP_SHA=$(sha256sum "$UPGRADE_BACKUP" | awk '{print $1}')
UPGRADE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$PREVIOUS_SHA"
RECORD="/opt/ticketwatch/data/upgrade-records/$UPGRADE_ID.env"
test ! -e "$RECORD" && test ! -L "$RECORD"
RECORD_TMP=$(mktemp /opt/ticketwatch/data/upgrade-records/.record.XXXXXX)
{
  printf 'PREVIOUS_SHA=%s\nNEW_SHA=%s\nWEB_IMAGE_ID=%s\nWORKER_IMAGE_ID=%s\n' "$PREVIOUS_SHA" "$NEW_SHA" "$WEB_IMAGE_ID" "$WORKER_IMAGE_ID"
  printf 'WEB_IMAGE_REF=%s\nWORKER_IMAGE_REF=%s\nBACKUP_PATH=%s\nBACKUP_SHA256=%s\n' "$WEB_IMAGE_REF" "$WORKER_IMAGE_REF" "$UPGRADE_BACKUP" "$UPGRADE_BACKUP_SHA"
} > "$RECORD_TMP"
install -o admin -g admin -m 0600 "$RECORD_TMP" "$RECORD"
rm -f -- "$RECORD_TMP"
docker compose --env-file /opt/ticketwatch/.env stop worker web
git fetch /opt/ticketwatch/migration-data/ticketwatch-release.bundle "$RELEASE_REF"
test "$(git rev-parse FETCH_HEAD)" = "$NEW_SHA"
git merge-base --is-ancestor "$NEW_SHA" FETCH_HEAD
git checkout --detach "$NEW_SHA"
test "$(git rev-parse HEAD)" = "$NEW_SHA"
docker image tag "$NEW_WEB_IMAGE" "$WEB_IMAGE_REF"
docker image tag "$NEW_WORKER_IMAGE" "$WORKER_IMAGE_REF"
docker compose --env-file /opt/ticketwatch/.env up -d --wait postgres redis --pull never --no-build
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build web python manage.py showmigrations core | grep -F '[X] 0009_agent_mail_verification_retry'
docker compose --env-file /opt/ticketwatch/.env up -d --no-deps --pull never --no-build web
curl --fail http://127.0.0.1:8000/healthz
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker python manage.py agent_mail_preflight
docker compose --env-file /opt/ticketwatch/.env up -d --no-deps --pull never --no-build worker
curl --fail http://127.0.0.1:8000/statusz
```

仅部署前后兼容的 schema 迁移。命令全部成功后还要做升级验收：确认运行提交和迁移精确匹配 manifest；确认 Web 的运行环境没有任何 `AGENTLY_*` 名称且镜像没有 CLI；确认 Worker 仍以 UID/GID 10001 运行、持有 CLI、身份预检和跨重启凭据均正常；确认 PostgreSQL、Redis、Worker 心跳、私有状态页和新备份恢复演练正常。以下检查不会打印环境变量值：

```bash
cd /opt/ticketwatch/app
test "$(git rev-parse HEAD)" = "$(sed -n 's/^RELEASE_SHA=//p' /opt/ticketwatch/migration-data/release-manifest.env)"
WEB_CONTAINER=$(docker compose --env-file /opt/ticketwatch/.env ps -q web)
docker inspect --format '{{json .Config.Env}}' "$WEB_CONTAINER" | python3 -c 'import json, sys; assert not any(item.split("=", 1)[0].startswith("AGENTLY_") for item in json.load(sys.stdin)); print("Web Agent Mail environment: isolated")'
docker compose --env-file /opt/ticketwatch/.env exec -T web sh -ec '! command -v agently-cli'
docker compose --env-file /opt/ticketwatch/.env exec -T worker sh -ec 'test "$(id -u):$(id -g)" = 10001:10001; command -v agently-cli >/dev/null'
docker compose --env-file /opt/ticketwatch/.env exec -T redis redis-cli ping | grep -qx PONG
docker compose --env-file /opt/ticketwatch/.env restart worker
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker python manage.py agent_mail_preflight
curl --fail http://127.0.0.1:8000/statusz
./deploy/backup-postgres.sh
LATEST_BACKUP=$(find /opt/ticketwatch/data/backups -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-)
test -n "$LATEST_BACKUP"
./deploy/verify-backup.sh "$LATEST_BACKUP"
docker compose --env-file /opt/ticketwatch/.env ps
```

最后通过 SSH 隧道里的生产 Web 页面创建一条真实监控任务，观察 Worker 领取检查、`next_check_at` 推进和通知去重。不能用 `runlocal`、Django shell 或直接写表代替这一步。

## 回滚

先区分应用故障与已确认的数据损坏。应用问题通常只切回上一 SHA 和保留的 image；不要因为一次应用回滚而恢复 PostgreSQL。选择升级时生成的记录文件；不要依赖已经丢失的终端变量。

```bash
cd /opt/ticketwatch/app
set -Eeuo pipefail
RECORD='/opt/ticketwatch/data/upgrade-records/REPLACE_WITH_SELECTED_RECORD.env'
test -f "$RECORD" && test ! -L "$RECORD"
test "$(stat -c '%U:%G:%a' "$RECORD")" = 'admin:admin:600'
for key in PREVIOUS_SHA NEW_SHA WEB_IMAGE_ID WORKER_IMAGE_ID WEB_IMAGE_REF WORKER_IMAGE_REF BACKUP_PATH BACKUP_SHA256; do
  test "$(grep -c "^$key=" "$RECORD")" = 1
done
test "$(grep -Ec '^(PREVIOUS_SHA|NEW_SHA|WEB_IMAGE_ID|WORKER_IMAGE_ID|WEB_IMAGE_REF|WORKER_IMAGE_REF|BACKUP_PATH|BACKUP_SHA256)=' "$RECORD")" = 8
record_value() { sed -n "s/^$1=//p" "$RECORD"; }
PREVIOUS_SHA=$(record_value PREVIOUS_SHA)
NEW_SHA=$(record_value NEW_SHA)
WEB_IMAGE_ID=$(record_value WEB_IMAGE_ID)
WORKER_IMAGE_ID=$(record_value WORKER_IMAGE_ID)
WEB_IMAGE_REF=$(record_value WEB_IMAGE_REF)
WORKER_IMAGE_REF=$(record_value WORKER_IMAGE_REF)
BACKUP_PATH=$(record_value BACKUP_PATH)
BACKUP_SHA256=$(record_value BACKUP_SHA256)
for sha in "$PREVIOUS_SHA" "$NEW_SHA"; do printf '%s\n' "$sha" | grep -Eq '^[0-9a-f]{40}$'; done
for image_id in "$WEB_IMAGE_ID" "$WORKER_IMAGE_ID"; do printf '%s\n' "$image_id" | grep -Eq '^sha256:[0-9a-f]{64}$'; done
for image_ref in "$WEB_IMAGE_REF" "$WORKER_IMAGE_REF"; do printf '%s\n' "$image_ref" | grep -Eq '^[A-Za-z0-9_./:@-]+$'; done
printf '%s\n' "$BACKUP_PATH" | grep -Eq '^/opt/ticketwatch/data/backups/ticketwatch-[0-9]{8}T[0-9]{6}Z\.sql\.gz$'
printf '%s\n' "$BACKUP_SHA256" | grep -Eq '^[0-9a-f]{64}$'
test "$(sha256sum "$BACKUP_PATH" | awk '{print $1}')" = "$BACKUP_SHA256"
docker image inspect "$WEB_IMAGE_ID" "$WORKER_IMAGE_ID" >/dev/null
test "$(git rev-parse HEAD)" = "$NEW_SHA"
docker compose --env-file /opt/ticketwatch/.env stop worker web
git checkout --detach "$PREVIOUS_SHA"
test "$(git rev-parse HEAD)" = "$PREVIOUS_SHA"
docker image tag "$WEB_IMAGE_ID" "$WEB_IMAGE_REF"
docker image tag "$WORKER_IMAGE_ID" "$WORKER_IMAGE_REF"
docker compose --env-file /opt/ticketwatch/.env up -d --pull never --no-build --no-deps web
curl --fail http://127.0.0.1:8000/healthz
docker compose --env-file /opt/ticketwatch/.env run --rm --pull never --no-build worker python manage.py agent_mail_preflight
docker compose --env-file /opt/ticketwatch/.env up -d --pull never --no-build --no-deps worker
curl --fail http://127.0.0.1:8000/statusz
```

若记录文件中的 image ID 被清理，只能在验证 `PREVIOUS_SHA` 后重新构建，并在 incident 记录中说明这一例外。

只有确认数据已损坏、指定备份已通过 `verify-backup.sh`，并且先完成故障现场备份后，才可运行下面这个显式破坏性步骤。它会删除并重建名为 `POSTGRES_DB` 的生产数据库；不要把它当作普通应用回滚。故障现场备份必须复制到 incident 目录，因为常规七天保留策略可能删除备份目录中的唯一副本。

```bash
cd /opt/ticketwatch/app
set -Eeuo pipefail
BACKUP='/opt/ticketwatch/data/backups/ticketwatch-YYYYMMDDTHHMMSSZ.sql.gz'
INCIDENT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
printf '%s\n' "$INCIDENT_ID" | grep -Eq '^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$'
INCIDENT_DIR="/opt/ticketwatch/data/incidents/$INCIDENT_ID"
FAILURE_SCENE_COPY="$INCIDENT_DIR/failure-scene.sql.gz"
FAILURE_SCENE_RECORD="$INCIDENT_DIR/failure-scene-record.txt"
umask 077
mkdir --mode=0700 -- "$INCIDENT_DIR"
test "$(stat -c '%U:%G:%a' "$INCIDENT_DIR")" = 'admin:admin:700'
test -f "$BACKUP"
./deploy/verify-backup.sh "$BACKUP"
./deploy/backup-postgres.sh
FAILURE_SCENE_BACKUP=$(find /opt/ticketwatch/data/backups -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -printf '%T@ %p\n' | sort -n | tail -n 1 | cut -d' ' -f2-)
test -n "$FAILURE_SCENE_BACKUP"
COPY_TMP=
RECORD_TMP=
cleanup_incident_temps() {
  local status=$?
  trap - EXIT
  for temporary in "${COPY_TMP:-}" "${RECORD_TMP:-}"; do
    if [[ -n "$temporary" && -e "$temporary" ]]; then
      rm -f -- "$temporary" || status=1
    fi
  done
  exit "$status"
}
trap cleanup_incident_temps EXIT
COPY_TMP=$(mktemp "$INCIDENT_DIR/.failure-scene.XXXXXX")
RECORD_TMP=$(mktemp "$INCIDENT_DIR/.failure-scene-record.XXXXXX")
cp --preserve=mode "$FAILURE_SCENE_BACKUP" "$COPY_TMP"
chown admin:admin "$COPY_TMP"
chmod 0600 "$COPY_TMP"
ln --no-target-directory -- "$COPY_TMP" "$FAILURE_SCENE_COPY"
rm -f -- "$COPY_TMP"
COPY_TMP=
cmp -s "$FAILURE_SCENE_BACKUP" "$FAILURE_SCENE_COPY"
FAILURE_SCENE_SHA=$(sha256sum "$FAILURE_SCENE_COPY" | awk '{print $1}')
printf 'failure_scene_backup=%s\nsha256=%s\n' "$FAILURE_SCENE_COPY" "$FAILURE_SCENE_SHA" > "$RECORD_TMP"
chmod 0600 "$RECORD_TMP"
ln --no-target-directory -- "$RECORD_TMP" "$FAILURE_SCENE_RECORD"
rm -f -- "$RECORD_TMP"
RECORD_TMP=
trap - EXIT
docker compose --env-file /opt/ticketwatch/.env stop worker web
gzip -dc -- "$BACKUP" | docker compose --env-file /opt/ticketwatch/.env exec -T postgres sh -ec 'dropdb --if-exists --no-password --username "$POSTGRES_USER" "$POSTGRES_DB"; createdb --no-password --username "$POSTGRES_USER" "$POSTGRES_DB"; pg_restore --exit-on-error --no-owner --no-acl --no-password --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"'
docker compose --env-file /opt/ticketwatch/.env run --rm --no-deps --pull never --no-build web python manage.py migrate
docker compose --env-file /opt/ticketwatch/.env up -d --pull never --no-build --no-deps web worker
curl --fail http://127.0.0.1:8000/statusz
```

恢复后核对业务表数量、活动任务及 `next_check_at`、通知唯一性、`0009_agent_mail_verification_retry` 和私有 UI。保留故障现场备份、恢复记录和本地 SQLite 原件。迁移 JSON/manifests 是明确临时目标；仅在验收后，以指定文件的方式清理，绝不对广泛路径执行删除。
