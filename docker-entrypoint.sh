#!/bin/sh
set -e

# Alte Image-CMD-Form (`CMD ["radio-ripper"]`): führenden Binärnamen entfernen –
# der Befehl wird unten vollständig neu aufgebaut.
if [ "$1" = "radio-ripper" ]; then
    shift
fi

CONFIG_PATH="${RADIO_RIPPER_CONFIG:-/app/config/config.json}"

# Gemountete Config nur injizieren, wenn kein --config/-c explizit gesetzt wurde.
config_set=0
for arg in "$@"; do
    case "$arg" in
        --config|-c|--config=*|-c*) config_set=1; break ;;
    esac
done

if [ "$config_set" -eq 0 ] && [ -f "$CONFIG_PATH" ]; then
    set -- --config "$CONFIG_PATH" "$@"
fi

# Befehl immer mit dem Binärnamen beginnen.
set -- radio-ripper "$@"

if [ "$(id -u)" = "0" ]; then
    chown -R ripper:ripper /app/destination /app/work /app/config 2>/dev/null || true
    # su verschluckt das erste Argument als $0 – deshalb `exec "$0" "$@"`.
    exec su -s /bin/sh -c 'exec "$0" "$@"' ripper -- "$@"
fi

exec "$@"
