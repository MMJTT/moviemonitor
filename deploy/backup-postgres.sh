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

mkdir -p "$BACKUP_DIR"
[[ -d "$WORK_DIR" && -f "$ENV_FILE" ]] || exit 2
[[ "$(realpath "$WORK_DIR")" == "$WORK_DIR" ]] || exit 2
[[ "$(realpath "$BACKUP_DIR")" == "$BACKUP_DIR" ]] || exit 2

set -a
. "$ENV_FILE"
set +a
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
cd "$WORK_DIR"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL_PATH="$BACKUP_DIR/ticketwatch-$STAMP.sql.gz"
[[ ! -e "$FINAL_PATH" ]] || exit 1

TEMP_PATH=$(mktemp "$BACKUP_DIR/.ticketwatch-backup.XXXXXX")
case "$TEMP_PATH" in
  "$BACKUP_DIR"/.ticketwatch-backup.??????) ;;
  *) exit 2 ;;
esac

cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  case "${TEMP_PATH:-}" in
    "$BACKUP_DIR"/.ticketwatch-backup.??????)
      if [[ -f "$TEMP_PATH" ]]; then
        rm -f -- "$TEMP_PATH" || true
      fi
      ;;
  esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose --env-file "$ENV_FILE" exec -T postgres \
  pg_dump --format=custom --no-owner --no-acl \
  --username "$POSTGRES_USER" "$POSTGRES_DB" | gzip -c >"$TEMP_PATH"
test -s "$TEMP_PATH"
mv -- "$TEMP_PATH" "$FINAL_PATH"
TEMP_PATH=

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -mtime +7 -delete
docker compose --env-file "$ENV_FILE" exec -T web \
  python manage.py mark_backup_success --name "${FINAL_PATH##*/}"
