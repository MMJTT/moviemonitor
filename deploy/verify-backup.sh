#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

WORK_DIR=/opt/ticketwatch/app
BACKUP_DIR=/opt/ticketwatch/data/backups
ENV_FILE=/opt/ticketwatch/.env

case "$WORK_DIR:$BACKUP_DIR:$ENV_FILE" in
  /opt/ticketwatch/app:/opt/ticketwatch/data/backups:/opt/ticketwatch/.env) ;;
  *) exit 2 ;;
esac
[[ $# -eq 1 && "$1" == /* ]] || exit 2
[[ -d "$WORK_DIR" && -d "$BACKUP_DIR" && -f "$ENV_FILE" ]] || exit 2
[[ "$(realpath "$WORK_DIR")" == "$WORK_DIR" ]] || exit 2
[[ "$(realpath "$BACKUP_DIR")" == "$BACKUP_DIR" ]] || exit 2

BACKUP_REAL=$(realpath "$1")
case "$BACKUP_REAL" in
  "$BACKUP_DIR"/*) ;;
  *) exit 2 ;;
esac
BACKUP_NAME=${BACKUP_REAL#"$BACKUP_DIR"/}
[[ "$BACKUP_NAME" != */* ]] || exit 2
[[ "$BACKUP_NAME" =~ ^ticketwatch-[0-9]{8}T[0-9]{6}Z\.sql\.gz$ ]] || exit 2
[[ -f "$BACKUP_REAL" ]] || exit 2

set -a
. "$ENV_FILE"
set +a
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
cd "$WORK_DIR"

printf -v RESTORE_DB 'ticketwatch_restore_%04x%04x%04x%04x' \
  "$RANDOM" "$RANDOM" "$RANDOM" "$RANDOM"
[[ "$RESTORE_DB" != "$POSTGRES_DB" ]] || exit 2
RESTORE_CREATED=0

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  if [[ "$RESTORE_CREATED" == 1 ]]; then
    RESTORE_CREATED=0
    docker compose --env-file "$ENV_FILE" exec -T postgres \
      dropdb --if-exists --username "$POSTGRES_USER" "$RESTORE_DB" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose --env-file "$ENV_FILE" exec -T postgres \
  createdb --username "$POSTGRES_USER" "$RESTORE_DB"
RESTORE_CREATED=1
gzip -dc "$BACKUP_REAL" | docker compose --env-file "$ENV_FILE" exec -T postgres \
  pg_restore --exit-on-error --no-owner --no-acl \
  --username "$POSTGRES_USER" --dbname "$RESTORE_DB"
docker compose --env-file "$ENV_FILE" exec -T postgres \
  psql --username "$POSTGRES_USER" --dbname "$RESTORE_DB" --tuples-only --command \
  'SELECT COUNT(*) FROM django_migrations; SELECT COUNT(*) FROM core_monitortask; SELECT COUNT(*) FROM core_checkrun; SELECT COUNT(*) FROM core_notification; SELECT COUNT(*) FROM core_appsetting; SELECT COUNT(*) FROM core_agentmailconfig;'
