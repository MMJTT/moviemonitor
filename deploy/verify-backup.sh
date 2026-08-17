#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

WORK_DIR=/opt/ticketwatch/app
DATA_DIR=/opt/ticketwatch/data
BACKUP_DIR=/opt/ticketwatch/data/backups
ENV_FILE=/opt/ticketwatch/.env

case "$WORK_DIR:$DATA_DIR:$BACKUP_DIR:$ENV_FILE" in
  /opt/ticketwatch/app:/opt/ticketwatch/data:/opt/ticketwatch/data/backups:/opt/ticketwatch/.env) ;;
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
  local line trimmed value
  local user_seen=0
  local db_seen=0
  POSTGRES_USER=
  POSTGRES_DB=

  while IFS= read -r line || [[ -n "$line" ]]; do
    trimmed=${line#"${line%%[![:space:]]*}"}
    case "$trimmed" in
      '' | '#'* ) continue ;;
    esac
    case "$line" in
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

[[ $# -eq 1 && "$1" == /* ]] || exit 2
validate_storage_boundary || exit 2
validate_trusted_directory "$WORK_DIR" || exit 2
validate_trusted_file "$ENV_FILE" || exit 2

BACKUP_REAL=$(realpath "$1")
case "$BACKUP_REAL" in
  "$BACKUP_DIR"/*) ;;
  *) exit 2 ;;
esac
BACKUP_NAME=${BACKUP_REAL#"$BACKUP_DIR"/}
[[ "$BACKUP_NAME" != */* ]] || exit 2
[[ "$BACKUP_NAME" =~ ^ticketwatch-[0-9]{8}T[0-9]{6}Z\.sql\.gz$ ]] || exit 2
validate_trusted_file "$BACKUP_REAL" || exit 2
exec 9<"$BACKUP_REAL"
[[ "$(realpath "/proc/$$/fd/9")" == "$BACKUP_REAL" ]] || exit 2

parse_postgres_identifiers || exit 2
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
    if ! docker compose --env-file "$ENV_FILE" exec -T postgres \
      dropdb --if-exists --no-password --username "$POSTGRES_USER" "$RESTORE_DB" \
      >/dev/null 2>&1; then
      if (( status == 0 )); then
        status=1
      fi
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

docker compose --env-file "$ENV_FILE" exec -T postgres \
  createdb --no-password --username "$POSTGRES_USER" "$RESTORE_DB"
RESTORE_CREATED=1
gzip -dc <&9 | docker compose --env-file "$ENV_FILE" exec -T postgres \
  pg_restore --exit-on-error --no-owner --no-acl --no-password \
  --username "$POSTGRES_USER" --dbname "$RESTORE_DB"
docker compose --env-file "$ENV_FILE" exec -T postgres \
  psql --no-password --username "$POSTGRES_USER" --dbname "$RESTORE_DB" \
  --tuples-only --command \
  'SELECT COUNT(*) FROM django_migrations; SELECT COUNT(*) FROM core_monitortask; SELECT COUNT(*) FROM core_checkrun; SELECT COUNT(*) FROM core_notification; SELECT COUNT(*) FROM core_appsetting; SELECT COUNT(*) FROM core_agentmailconfig;'
