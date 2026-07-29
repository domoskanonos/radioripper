#!/bin/sh
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R ripper:ripper /app/mp3_inbox /app/work /app/config 2>/dev/null || true
    exec su -s /bin/sh -c 'exec "$@"' ripper -- "$@"
fi

if [ -f /app/config/config.json ]; then
    set -- radio-ripper --config /app/config/config.json "$@"
fi

exec "$@"
