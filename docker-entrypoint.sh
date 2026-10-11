#!/bin/sh
# Runs as root only long enough to make the data volume writable, then drops privileges.
set -e
PUID="${PUID:-10001}"
PGID="${PGID:-10001}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "${PT_DATA_DIR:-/data}"
  chown -R "$PUID:$PGID" "${PT_DATA_DIR:-/data}"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi
exec "$@"
