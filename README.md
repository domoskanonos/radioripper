# Radio-Ripper – Stream

Dauerhafte parallele Aufnahme von Webradio-Streams mit ICY-Metadaten (Songtitel).
Erkennt und parst ICY-Stream-Metadaten, trennt Aufnahmen an Songgrenzen, verwaltet hunderte parallele Streams und unterstützt die automatische Sendersuche via Community-Playlists.

## Features

- **Parallel-Recording** – zeichnet bis zu 500 Streams gleichzeitig auf
- **ICY-Metadaten** – erkennt Songtitel in Echtzeit, trennt Aufnahmen sauber an Songgrenzen
- **Auto-Healing** – bei Verbindungsabbruch automatische Wiederverbindung mit exponentiellem Backoff
- **Song-Titelerkennung** – benennt jede Aufnahme als `{Künstler} - {Titel}.mp3`
- **Werbefilter** – überspringt Titel per Regex-Muster (z. B. `["^Werbung", "^Advertisement$"]`)
- **Datei-Validierung** – verwirft zu kurze (< 90 s), zu kleine (< 1,5 MB) oder ungültige MP3-Dateien
- **AcoustID-Filterung** – behalte nur Aufnahmen, die einen bekannten AcoustID-Fingerprint-Score erreichen (konfigurierbar, Standard 0,9); erfordert `ACOUST_ID` API-Key und `fpcalc`
- **Inbox-Überwachung** – pausiert alle Streams bei vollem Zielverzeichnis, setzt automatisch fort
- **Hot-Reload** – Konfigurationsänderungen werden live übernommen (alle 60 s), kein Neustart nötig
- **Auto-Discovery** – findet Sender automatisch aus Community-Playlists, filtert nach Keywords und Bitrate
- **Preflight-Check** – prüft alle Stationen vor dem Start auf Erreichbarkeit, deaktiviert tote Stationen
- **Pre-filtered Cache** – einmal geprüfte Stationen werden gecached für schnelle Neustarts
- **Graceful Shutdown** – schließt HTTP-Client sofort, beendet alle recorder parallel (< 15 s)

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

# Container starten (ACOUST_ID ist Pflicht)
docker run --rm \
  -e ACOUST_ID=dein_acoustid_api_key \
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

### docker compose

```yaml
services:
  radioripper:
    image: domoskanonos/radio-ripper-stream:latest
    container_name: radio-ripper
    restart: unless-stopped
    stop_signal: SIGTERM
    stop_grace_period: 30s
    healthcheck:
      test: ["CMD-SHELL", "pgrep -f 'radio-ripper' || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    environment:
      # AcoustID API-Key – PFLICHTFELD (https://acoustid.org/login)
      ACOUST_ID: "dein_acoustid_api_key"
    volumes:
      # Konfiguration (read-only)
      - ./radio-ripper-config:/app/config:ro
      # Zielverzeichnis für MP3-Aufnahmen
      - ./radio-ripper-mp3:/app/destination
      # Arbeitsverzeichnis (Cache, Logs)
      - ./radio-ripper-work:/app/work
```

## Umgebungsvariablen

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `ACOUST_ID` | **ja** | AcoustID API-Key. Ohne diesen Wert startet radio-ripper nicht. Kostenlos registrieren unter [acoustid.org/login](https://acoustid.org/login). |
| `GITHUB_PAT` | nein | GitHub Personal Access Token für den Download der Community-M3U-Playlist. |

## Konfiguration (`config/config.json`)

Alle Felder sind optional – es gelten die gezeigten Defaults.

| Feld | Typ | Standard | Beschreibung |
|---|---|---|---|
| `work_dir` | string | `./work` | Arbeitsverzeichnis für Logs, Caches und Playlists |
| `destination` | string | `./destination` | Zielverzeichnis für fertige MP3-Aufnahmen |
| `log_level` | string | `"INFO"` | Log-Level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `max_concurrent_streams` | integer | `400` | Maximale parallele Streams (1–500) |
| `stream_keywords` | string[] | `["rock","pop","top hits",…]` | Suchbegriffe für die automatische Sendersuche |
| `discovery_enabled` | boolean | `true` | Automatische Sendersuche aktivieren |
| `discovery_min_bitrate` | integer | `0` | Minimale Bitrate (kbps) für entdeckte Sender |
| `streams` | array | `[]` | Liste fester Sender (überspringt Discovery). Format: `[{"name":"…","url":"http://…"}]` |
| `request_timeout` | number | `30.0` | Timeout für HTTP-Requests (Sekunden) |
| `reconnect_base_delay` | number | `1.0` | Basisverzögerung vor Wiederverbindung (Sekunden) |
| `reconnect_max_delay` | number | `60.0` | Maximale Wiederverbindungsverzögerung |
| `user_agent` | string | `"Radio-Ripper/2.0"` | User-Agent für HTTP-Requests |
| `no_icy_disable_after` | integer | `10` | Nach wie vielen ICY-freien Verbindungen ein Stream deaktiviert wird |
| `ignore_title_patterns` | string[] | `[]` | Regex-Muster für zu ignorierende Songtitel (z. B. `["^Werbung"]`) |
| `min_file_size_bytes` | integer | `1572864` | Aufnahmen kleiner als dieser Wert (1,5 MB) werden verworfen |
| `min_file_duration_s` | float | `90` | Mindestlaufzeit einer Aufnahme (Sekunden); erfordert `ffprobe` |
| `max_files_inbox` | integer | `100000` | Max. Dateien im Zielverzeichnis; bei Erreichen pausieren alle Streams |
| `acoustid_requests_per_minute` | integer | `170` | Max. AcoustID-API-Aufrufe pro Minute (Limit: 180 = 3 req/s) |
| `acoustid_min_score` | float | `0.9` | Mindest-AcoustID-Score für behaltene Aufnahmen (0.0–1.0) |
| `acoustid_retry_max_attempts` | integer | `5` | Wiederholungen bei transienten AcoustID-Fehlern |
| `acoustid_retry_base_delay` | float | `30.0` | Start-Wartezeit vor Retry (Sekunden) |
| `acoustid_retry_max_delay` | float | `3600.0` | Maximale Wartezeit zwischen Retries (Sekunden) |
| `max_unchecked_files` | integer | `10000` | Datei-Obergrenze in `work/unchecked_mp3` |
| `max_unchecked_bytes` | integer | `10737418240` | Größen-Obergrenze für `work/unchecked_mp3` (10 GB) |

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

### Stream-Konfiguration (pro Eintrag in `streams[]`)

| Feld | Typ | Standard | Beschreibung |
|---|---|---|---|
| `name` | string | – | Name des Senders (Pflichtfeld, max. 64 Zeichen) |
| `url` | string | – | Playlist-URL (M3U/PLS) oder direkte Stream-URL |
| `enabled` | boolean | `true` | Sender aktivieren/deaktivieren |
| `ignore_title_patterns` | string[] | `null` | Sendereigene Regex-Muster (überschreibt globale) |
| `bitrate` | integer | `0` | Bekannte Bitrate in kbps (0 = unbekannt) |
| `icy` | boolean | `true` | ICY-Metadaten erwartet |
| `source` | string | `""` | Quelle (z. B. `"discovery"`, `"custom"`) |

## Architektur (arc42)

### Aufbau der Aufnahmen

Jede Aufnahme durchläuft folgenden Lebenszyklus:

1. **TCP-Verbindung** zum Stream-Server (mit `Icy-MetaData: 1` Header)
2. **Metadaten-Parsing**: Der `IcyParser` extrahiert `StreamTitle` aus dem Datenstrom
3. **Song-Erkennung**: Bei Titelwechsel wird die vorherige Datei abgeschlossen und eine neue gestartet
4. **Temporäre Datei**: Die Aufnahme wird zuerst in eine `.tmp`-Datei im System-Temp geschrieben
5. **Commit**: Bei Titelwechsel wird die `.tmp`-Datei als `{Künstler} - {Titel}.mp3` ins Zielverzeichnis verschoben
6. **Validierung**: Die fertige MP3 wird auf Größe, Dauer und Gültigkeit geprüft – zu kurze oder ungültige Dateien werden automatisch gelöscht. Anschließend prüft **AcoustID** per Chromaprint-Fingerprint, ob der Track einem bekannten Song entspricht (Score ≥ 0,7); Aufnahmen ohne ausreichenden Match werden ebenfalls verworfen.

### Stream-Recorder Lebenszyklus

Jeder Stream wird von einem eigenen `StreamRecorder` verwaltet:

```
_start_forever()
  ├─ pause() / resume()           # Bei vollem Zielverzeichnis
  ├─ _run_once()
  │   ├─ Playlist auflösen (M3U/PLS → Stream-URL)
  │   ├─ _connect_stream()
  │   │   ├─ HTTP-GET mit Icy-MetaData: 1
  │   │   └─ ICY-Metaint parsen
  │   └─ _stream_with_meta()
  │       ├─ async for chunk: Parser füttern
  │       ├─ TitleChanged → alte Datei committen, neue starten
  │       └─ Verbindungsabbruch → _run_once returned False
  └─ Reconnect mit exponentiellem Backoff + Jitter
```

**Fehlertoleranz**:
- Bei Verbindungsabbruch bis zu `no_icy_disable_after`× wiederholen
- Nach `no_icy_disable_after` ICY-freien Streams wird der Sender deaktiviert
- Bei generischen Fehlern wird der Reconnect-Backoff verdoppelt (max. `reconnect_max_delay`)

### Auto-Discovery Pipeline

```
start()
  ├─ _select_stations()
  │   ├─ Explizite streams[] → direkt verwenden
  │   └─ custom.m3u laden → falls leer: leere Datei anlegen
  ├─ PlaylistDiscoveryService.load_or_discover()
  │   ├─ prefiltered.m3u existiert? → laden + filtern
  │   ├─ Sonst: Community-M3U von github.com/radiosure laden
  │   ├─ Alle Einträge mit ICY-Probe testen
  │   ├─ Ergebnisse in prefiltered.m3u cachen
  │   └─ Nach Keywords filtern oder zufällig auswählen
  ├─ _apply_stream_limit()
  │   └─ custom.m3u-Stationen priorisieren
  └─ _preflight_check()
      └─ Alle Stationen vor Start auf ICY-Erreichbarkeit prüfen (mit Fortschritts-Log alle 10 %)
```

### Verhalten bei vollem Zielverzeichnis

Wenn die Anzahl der `.mp3`-Dateien im Zielverzeichnis `max_files_inbox` erreicht:

1. **Alle Streams pausieren** – der aktuell laufende Song wird noch fertig geschrieben
2. **Alle 5 Minuten prüfen**, ob der Platz wieder reicht
3. **Automatisch weitermachen**, sobald die Dateianzahl auf ≤80 % des Limits gefallen ist

So wird verhindert, dass bei einem vollen Verzeichnis sinnlos Daten geschrieben werden.

### Signal- und Shutdown-Verhalten

| Signal | Verhalten |
|---|---|
| `SIGINT` / `SIGTERM` | Alle Recorder stoppen, HTTP-Client schließen, parallel joinen (< 15 s) |
| `KeyboardInterrupt` | Graceful Shutdown via `asyncio.run()` |

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
# Systemabhängigkeiten (Ubuntu/Debian)
sudo apt-get install ffmpeg libchromaprint-tools   # fpcalc für AcoustID-Fingerprinting

# macOS
brew install ffmpeg chromaprint

# Abhängigkeiten installieren
uv sync

# Linting & Typ-Prüfung
uv run ruff check src/radio_ripper/ tests/
uv run ruff format --check src/radio_ripper/ tests/
uv run mypy src/radio_ripper/

# Tests
uv run pytest

# Manueller Start (Config liegt in config/config.json)
ACOUST_ID=dein_api_key uv run radio-ripper --config config/config.json
```

## Lizenz

MIT
