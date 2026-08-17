#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

WORK_DIR=/opt/ticketwatch/app
DATA_DIR=/opt/ticketwatch/data
BACKUP_DIR=/opt/ticketwatch/data/backups
ENV_FILE=/opt/ticketwatch/.env
LOCK_PATH=/opt/ticketwatch/data/backups/.ticketwatch-backup.lock

case "$WORK_DIR:$DATA_DIR:$BACKUP_DIR:$ENV_FILE:$LOCK_PATH" in
  /opt/ticketwatch/app:/opt/ticketwatch/data:/opt/ticketwatch/data/backups:/opt/ticketwatch/.env:/opt/ticketwatch/data/backups/.ticketwatch-backup.lock) ;;
  *) exit 2 ;;
esac

validate_trusted_metadata() {
  local path=$1
  local owner mode
  owner=$(stat -c '%U' -- "$path") || return 1
  mode=$(stat -c '%a' -- "$path") || return 1
  case "$owner" in
    root | admin) ;;
    *) return 1 ;;
  esac
  [[ "$mode" =~ ^[0-7]{3,4}$ ]] || return 1
  (( (8#$mode & 8#022) == 0 ))
}

validate_trusted_directory() {
  local path=$1
  [[ -d "$path" && ! -L "$path" ]] || return 1
  [[ "$(realpath "$path")" == "$path" ]] || return 1
  validate_trusted_metadata "$path"
}

validate_trusted_file() {
  local path=$1
  [[ -f "$path" && ! -L "$path" ]] || return 1
  [[ "$(realpath "$path")" == "$path" ]] || return 1
  validate_trusted_metadata "$path"
}

validate_storage_boundary() {
  validate_trusted_directory /opt
  validate_trusted_directory /opt/ticketwatch
  validate_trusted_directory "$DATA_DIR"
  validate_trusted_directory "$BACKUP_DIR"
}

parse_postgres_identifiers() {
  local line value
  local user_seen=0
  local db_seen=0
  POSTGRES_USER=
  POSTGRES_DB=

  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      '' | [[:space:]]*'#'*) continue ;;
      POSTGRES_USER=*)
        (( user_seen == 0 )) || return 1
        value=${line#POSTGRES_USER=}
        [[ "$value" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || return 1
        POSTGRES_USER=$value
        user_seen=1
        ;;
      POSTGRES_DB=*)
        (( db_seen == 0 )) || return 1
        value=${line#POSTGRES_DB=}
        [[ "$value" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || return 1
        POSTGRES_DB=$value
        db_seen=1
        ;;
      *)
        if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?POSTGRES_(USER|DB) ]]; then
          return 1
        fi
        ;;
    esac
  done <"$ENV_FILE"
  (( user_seen == 1 && db_seen == 1 ))
}

validate_trusted_directory /opt || exit 2
validate_trusted_directory /opt/ticketwatch || exit 2
if [[ ! -e "$DATA_DIR" && ! -L "$DATA_DIR" ]]; then
  mkdir -- "$DATA_DIR"
fi
validate_trusted_directory "$DATA_DIR" || exit 2
if [[ ! -e "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]]; then
  mkdir -- "$BACKUP_DIR"
fi
validate_storage_boundary || exit 2
validate_trusted_directory "$WORK_DIR" || exit 2
validate_trusted_file "$ENV_FILE" || exit 2
parse_postgres_identifiers || exit 2
cd "$WORK_DIR"

if [[ ! -e "$LOCK_PATH" && ! -L "$LOCK_PATH" ]]; then
  if ! (set -o noclobber; : >"$LOCK_PATH") 2>/dev/null; then
    [[ -e "$LOCK_PATH" ]] || exit 2
  fi
fi
validate_trusted_file "$LOCK_PATH" || exit 2
exec 7<"$LOCK_PATH"
[[ "$(realpath "/proc/$$/fd/7")" == "$LOCK_PATH" ]] || exit 2
flock -x 7
validate_storage_boundary || exit 2

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FINAL_PATH="$BACKUP_DIR/ticketwatch-$STAMP.sql.gz"
[[ ! -e "$FINAL_PATH" && ! -L "$FINAL_PATH" ]] || exit 1

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
  pg_dump --format=custom --no-owner --no-acl --no-password \
  --username "$POSTGRES_USER" "$POSTGRES_DB" | gzip -c >"$TEMP_PATH"
test -s "$TEMP_PATH"
validate_storage_boundary || exit 2
ln -- "$TEMP_PATH" "$FINAL_PATH"
rm -f -- "$TEMP_PATH"
TEMP_PATH=

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'ticketwatch-*.sql.gz' -mtime +7 -delete
docker compose --env-file "$ENV_FILE" exec -T web \
  python manage.py mark_backup_success --name "${FINAL_PATH##*/}"
