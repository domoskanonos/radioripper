# Radio-Ripper – Stream

Dauerhafte parallele Aufnahme von Webradio-Streams mit ICY-Metadaten (Songtitel).  
Erkennt und parst ICY-Stream-Metadaten, trennt Aufnahmen an Songgrenzen, verwaltet parallele Streams und unterstützt die automatische Sendersuche via Community-Playlists.

## Schnellstart (Docker)

```bash
# Konfiguration vorbereiten (optional – ohne Config gelten Defaults)
mkdir -p radio-ripper-config radio-ripper-mp3 radio-ripper-work
cat > radio-ripper-config/config.json <<'EOF'
{
  "stream_keywords": ["rock", "pop", "jazz"],
  "max_concurrent_streams": 5
}
EOF

# Container starten
docker run --rm \
  -v "$PWD/radio-ripper-config:/app/config:ro" \
  -v "$PWD/radio-ripper-mp3:/app/destination" \
  -v "$PWD/radio-ripper-work:/app/work" \
  domoskanonos/radio-ripper-stream:latest
```

**Volume-Berechtigungen**: Der Container läuft als unprivilegierter User (`ripper`, uid 1000).  
Der Entrypoint korrigiert automatisch die Besitzer der gemounteten Verzeichnisse.  
Sollten dennoch Permission-Fehler auftreten, einmalig ausführen:

```bash
chown -R 1000:1000 radio-ripper-mp3 radio-ripper-work radio-ripper-config
```

Wird kein `--config` übergeben, startet die App mit den Code-Defaults.  
Der Entrypoint prüft automatisch, ob `/app/config/config.json` existiert, und übergibt es.
Ein eigenes Config-JSON kann unter `config/config.json` (bzw. `/app/config/config.json` im Container) bereitgestellt werden.

### docker compose

```yaml
services:
  radioripper:
    image: domoskanonos/radio-ripper-stream:latest
    container_name: radio-ripper
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pgrep -f 'radio-ripper' || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    stop_signal: SIGTERM
    stop_grace_period: 30s
    volumes:
      - ./radio-ripper-config:/app/config:ro
      - ./radio-ripper-mp3:/app/destination
      - ./radio-ripper-work:/app/work
```

## Konfiguration (`config/config.json`)

Die Konfigurationsdatei steuert alle Aspekte des Recordings. Alle Felder sind optional – es gelten die gezeigten Defaults.

| Feld | Typ | Standard | Beschreibung |
|---|---|---|---|
| `work_dir` | string | `./work` | Arbeitsverzeichnis (Cache, Logs) |
| `destination` | string | `./destination` | Zielverzeichnis für fertige MP3-Aufnahmen |
| `log_level` | string | `"INFO"` | Einer von `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `max_concurrent_streams` | integer | `400` | Maximale parallele Streams (1–500) |
| `stream_keywords` | string[] | `["rock","pop","top hits",…]` | Suchbegriffe für die Sendersuche |
| `discovery_enabled` | boolean | `true` | Automatische Sendersuche aktivieren |
| `discovery_min_bitrate` | integer | `0` | Minimale Bitrate für entdeckte Sender |
| `streams` | array | `[]` | Liste fester Sender (überspringt Discovery). Format: `[{"name":"…","url":"http://…"}]` |
| `request_timeout` | number | `30.0` | Timeout für HTTP-Requests (Sekunden) |
| `reconnect_base_delay` | number | `1.0` | Basisverzögerung vor Wiederverbindung (Sekunden) |
| `reconnect_max_delay` | number | `60.0` | Maximale Wiederverbindungsverzögerung |
| `user_agent` | string | `"Radio-Ripper/2.0"` | User-Agent für HTTP-Requests |
| `no_icy_disable_after` | integer | `10` | Nach wie vielen ICY-freien Verbindungen ein Stream deaktiviert wird |
| `ignore_title_patterns` | string[] | `[]` | Regex-Muster für zu ignorierende Songtitel (z. B. `["^Werbung"]`) |
| `min_file_size_bytes` | integer | `1572864` | Aufnahmen kleiner als dieser Wert (1,5 MB) werden verworfen |
| `min_file_duration_s` | float | `90` | Mindestlaufzeit einer Aufnahme (Sekunden); erfordert `ffprobe` |
| `max_files_inbox` | integer | `100000` | Max. Dateien im Inbox-Verzeichnis; bei Erreichen pausieren alle Streams |

### Live-Config (Hot-Reload)

Die App prüft **alle 60 Sekunden**, ob `config.json` geändert wurde.  
Erkannte Änderungen werden **live übernommen** – kein Neustart nötig.

**Hot-reloadbare Felder:**
- `log_level` – wird sofort gesetzt
- `stream_keywords`, `discovery_enabled` – wirken beim nächsten Discovery-Durchlauf
- `request_timeout`, `reconnect_base_delay`, `reconnect_max_delay` – gelten für neue Verbindungen
- `no_icy_disable_after`, `ignore_title_patterns` – werden bei nächster Gelegenheit aktiv
- `min_file_size_bytes`, `min_file_duration_s` – gelten für die nächste Datei-Validierung
- `max_files_inbox` – neuer Schwellwert für den Inbox-Monitor

**Nicht hot-reloadbar** (erfordern Neustart): `work_dir`, `destination`, `streams`-Liste, `user_agent`.

### Beispiel: Feste Sender + Discovery

```json
{
  "streams": [
    {"name": "Mein Radio", "url": "http://example.com/stream.mp3", "enabled": true, "bitrate": 128}
  ],
  "stream_keywords": ["indie", "alternative"],
  "max_concurrent_streams": 8
}
```

## Verhalten bei vollem Inbox-Verzeichnis

Wenn die Anzahl der `.mp3`-Dateien im Inbox-Verzeichnis `max_files_inbox` erreicht:

1. **Alle Streams pausieren** – der aktuell laufende Song wird noch fertig geschrieben
2. **Alle 5 Minuten prüfen**, ob der Platz wieder reicht
3. **Automatisch weitermachen**, sobald die Dateianzahl auf ≤80 % des Limits gefallen ist

So wird verhindert, dass bei einem vollen Verzeichnis sinnlos Daten geschrieben werden.

## Aufbau der Aufnahmen

- Jede Aufnahme landet als `{Künstler} - {Titel}.mp3` im Inbox-Verzeichnis
- Temporäre Dateien werden im System-Temp (`/tmp`) angelegt und beim Commit verschoben
- Zu kurze, zu kleine oder ungültige MP3-Dateien werden automatisch verworfen
- Die Discovery-Cache-Datei wird im Arbeitsverzeichnis (`work_dir`) gespeichert

## Image-Tags

| Tag | Beschreibung |
|---|---|
| `latest` | Neuester Stand des `main`-Branches |
| `2.x` | Semantische Versions-Tags |
| `YYYYMMDD-<sha>` | Tägliche SHA-basierte Tags |

Alle Images laufen unter einem unprivilegierten Benutzer (`ripper`, uid 1000).  
Der Container-Entrypoint ist `radio-ripper` – du kannst Argumente wie `--config /pfad/config.json` oder `--log-level DEBUG` anhängen.

## Entwicklung

```bash
# Abhängigkeiten installieren
uv sync

# Linting & Typ-Prüfung
uv run ruff check src/radio_ripper/ tests/
uv run ruff format --check src/radio_ripper/ tests/
uv run mypy src/radio_ripper/

# Tests
uv run pytest

# Manueller Start (Config liegt in config/config.json)
uv run radio-ripper --config config/config.json
```

## Lizenz

MIT
