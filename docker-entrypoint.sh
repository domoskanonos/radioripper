#!/bin/sh
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R ripper:ripper /app/recordings /app/work /app/config 2>/dev/null || true
    exec su -s /bin/sh -c 'exec "$@"' ripper -- "$@"
fi

exec "$@"
