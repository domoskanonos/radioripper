#!/usr/bin/env bash
# run.sh - Startet den Radio-Ripper im Vordergrund mit Live-Log auf der Konsole.
# Strg+C beendet den Prozess sauber (Graceful Shutdown wird ans Python weitergereicht).
#
# Voraussetzung: uv (https://docs.astral.sh/uv/)
# Konfiguration: ./config.json  (Pfad unten via CONFIG= änderbar)
set -euo pipefail

# ----------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG="${CONFIG:-config.json}"
PID_FILE="./radio_ripper.pid"

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
log()  { printf '\033[1;34m[run.sh]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[run.sh]\033[0m %s\n' "$*" >&2; }
err()  { printf '\033[1;31m[run.sh]\033[0m %s\n' "$*" >&2; }

_CLEANUP_RAN=0
cleanup() {
  [[ "$_CLEANUP_RAN" -eq 1 ]] && return
  _CLEANUP_RAN=1
  trap - INT TERM EXIT
  local rc=$?
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    log "Signal an Ripper (PID $PID) weiterleiten - Graceful Shutdown..."
    kill -TERM "$PID" 2>/dev/null || true
    for _ in {1..60}; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$PID" 2>/dev/null; then
      warn "Prozess reagiert nicht - sende SIGKILL."
      kill -KILL "$PID" 2>/dev/null || true
    fi
    wait "$PID" 2>/dev/null || true
    rc=$?
  fi
  rm -f "$PID_FILE"
  log "Fertig. (Exit $rc)"
  exit "$rc"
}

# ----------------------------------------------------------------------
# Pre-Checks
# ----------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  err "uv wurde nicht gefunden. Bitte installieren:"
  err "    curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  err "Konfigurationsdatei nicht gefunden: $CONFIG"
  err "Erstelle eine config.json (siehe README) oder setze CONFIG=<pfad>."
  exit 1
fi

# Doppelstart-Schutz
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    err "Radio-Ripper laeuft bereits (PID $OLD_PID)."
    err "Stoppen mit:  kill -TERM $OLD_PID"
    exit 1
  else
    warn "Verwaestes PID-File gefunden - entferne es."
    rm -f "$PID_FILE"
  fi
fi

# ----------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------
log "Abhaengigkeiten synchronisieren (uv sync)..."
uv sync --quiet

log "Starte Radio-Ripper (Vordergrund, Live-Log folgt)..."
log "Config : $CONFIG"
log "Stop   : Strg+C"
echo "----------------------------------------------------------------"

# ----------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------
trap cleanup INT TERM EXIT

# Prozess in eigener Prozessgruppe starten, damit Strg+C sauber weitergeleitet
# wird. Flask/Python haengt SIGINT/SIGTERM selbstaendig ab.
uv run radio-ripper --config "$CONFIG" &
PID=$!
echo "$PID" > "$PID_FILE"

# Live-Ausgabe: warten, bis der Prozess endet. stdout/stderr des Kindes
# laeuft direkt ins Terminal, da wir nicht umleiten.
wait "$PID"
RC=$?

# trap uebernimmt Cleanup - rc wird dort weitergereicht.
exit "$RC"