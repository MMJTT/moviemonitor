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
