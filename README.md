# Radio-Ripper – Stream

Dauerhafte parallele Aufnahme von Webradio-Streams mit ICY-Metadaten (Songtitel).  
Erkennt und parst ICY-Stream-Metadaten, trennt Aufnahmen an Songgrenzen (optional), verwaltet parallele Streams und unterstützt die automatische Sendersuche via Community-Playlists.

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
  -v "$PWD/radio-ripper-mp3:/app/mp3_inbox" \
  -v "$PWD/radio-ripper-work:/app/work" \
  domoskanonos/radio-ripper-stream:latest
```

**Volume-Berechtigungen**: Der Container läuft als unprivilegierter User (`ripper`, uid 1001).  
Der Entrypoint korrigiert automatisch die Besitzer der gemounteten Verzeichnisse.  
Sollten dennoch Permission-Fehler auftreten, einmalig ausführen:

```bash
chown -R 1001:1001 radio-ripper-mp3 radio-ripper-work radio-ripper-config
```

Wird kein `--config` angegeben, startet die App mit den Code-Defaults.  
Ein eigenes Config-JSON kann bei Bedarf unter `/app/config/config.json` ins Image gemountet werden.

### docker compose

```yaml
services:
  radioripper:
    image: domoskanonos/radio-ripper-stream:latest
    container_name: radio-ripper
    restart: unless-stopped
    volumes:
      - ./radio-ripper-config:/app/config:ro
      - ./radio-ripper-mp3:/app/mp3_inbox
      - ./radio-ripper-work:/app/work
```

## Konfiguration (`config.json`)

Die Konfigurationsdatei steuert alle Aspekte des Recordings. Alle Felder sind optional – es gelten die gezeigten Defaults.

| Feld | Typ | Standard | Beschreibung |
|---|---|---|---|---|
| `mp3_inbox` | string | `/app/mp3_inbox` | Zielverzeichnis für fertige MP3-Aufnahmen |
| `work_dir` | string | `/app/work` | Arbeitsverzeichnis (Cache, DB, Logs) |
| `log_level` | string | `"INFO"` | Einer von `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `max_concurrent_streams` | integer | `400` | Maximale parallele Streams |
| `request_timeout` | number | `30` | Timeout für HTTP-Requests (Sekunden) |
| `reconnect_base_delay` | number | `1.0` | Basisverzögerung vor Wiederverbindung (Sekunden) |
| `reconnect_max_delay` | number | `60.0` | Maximale Wiederverbindungsverzögerung |
| `min_file_size_bytes` | integer | `4096` | Aufnahmen kleiner als dieser Wert werden verworfen |
| `min_file_duration_s` | float | `90` | Mindestlaufzeit einer Aufnahme (Sekunden); erfordert `ffprobe` |
| `user_agent` | string | `"Radio-Ripper-Stream/2.0"` | User-Agent für HTTP-Requests |
| `no_icy_disable_after` | integer | `15` | Nach wie vielen ICY-freien Verbindungen ein Stream deaktiviert wird |
| `discovery_enabled` | boolean | `true` | Automatische Sendersuche aktivieren |
| `stream_keywords` | string[] | `["rock","pop","top hits",…]` | Suchbegriffe für die Sendersuche |
| `discovery_min_bitrate` | integer | `0` | Minimale Bitrate für entdeckte Sender |
| `ignore_title_patterns` | string[] | `[]` | Regex-Muster für zu ignorierende Songtitel (z. B. `["^Werbung", "News \d"]`) |
| `streams` | array | `[]` | Liste fester Sender (überspringt Discovery). Format: `[{"name":"Mein Sender","url":"http://…"}]` |

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

## Aufbau der Aufnahmen

- Jeder Stream bekommt einen eigenen Unterordner im Zielverzeichnis.
- Dateiname: `{Künstler} - {Titel}.mp3` (bei ICY-Metadaten) oder `{timestamp}.mp3`.
- Temporäre Dateien landen im Arbeitsverzeichnis und werden beim Commit der Aufnahme ins Ziel verschoben.
- Zu kurze oder zu kleine Dateien werden automatisch verworfen.

## Image-Tags

| Tag | Beschreibung |
|---|---|
| `latest` | Neuester Stand des `main`-Branches |
| `2.x` | Semantische Versions-Tags |
| `YYYYMMDD-<sha>` | Tägliche SHA-basierte Tags |

Alle Images laufen unter einem unprivilegierten Benutzer (`ripper`, uid 1001).  
Der Container-Entrypoint ist `radio-ripper` – du kannst Argumente wie `--config /pfad/config.json` oder `--log-level DEBUG` anhängen.

## Entwicklung

```bash
# Abhängigkeiten installieren
uv sync

# Linting & Typ-Prüfung
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/radio_ripper/

# Tests
uv run pytest

# Manueller Start
uv run radio-ripper --config config.json
```

## Lizenz

MIT
