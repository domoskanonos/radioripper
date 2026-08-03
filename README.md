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
- **Backpressure** – pausiert alle Streams bei vollem Zielverzeichnis oder überfüllter AcoustID-Queue (`work/unchecked_mp3`), setzt automatisch fort
- **Hot-Reload** – Konfigurationsänderungen werden alle 60 s übernommen; nur die betroffenen Recorder werden neu gestartet, kein Container-Neustart nötig
- **Auto-Discovery** – findet Sender automatisch aus Community-Playlists, filtert nach Keywords und Bitrate
- **Preflight-Check** – prüft alle Stationen vor dem Start auf Erreichbarkeit, deaktiviert tote Stationen
- **Pre-filtered Cache** – einmal geprüfte Stationen werden gecached für schnelle Neustarts
- **Graceful Shutdown** – beendet alle Recorder parallel, schließt HTTP-Clients und AcoustID-Queue (< 15 s)

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
| `user_agent` | string | `Radio-Ripper/<Version>` | User-Agent für HTTP-Requests |
| `no_icy_disable_after` | integer | `10` | Nach wie vielen ICY-freien Verbindungen ein Stream deaktiviert wird |
| `ignore_title_patterns` | string[] | `[]` | Regex-Muster für zu ignorierende Songtitel (z. B. `["^Werbung"]`) |
| `min_file_size_bytes` | integer | `1572864` | Aufnahmen kleiner als dieser Wert (1,5 MB) werden verworfen |
| `min_file_duration_s` | float | `90` | Mindestlaufzeit einer Aufnahme (Sekunden); erfordert `ffprobe` |
| `max_files_inbox` | integer | `100000` | Max. Dateien im Zielverzeichnis; bei Erreichen pausieren alle Streams |
| `probe_timeout` | number | `8.0` | Timeout der ICY-Probe beim Preflight-Check (Sekunden) |
| `probe_concurrent` | integer | `20` | Parallele Preflight-Proben (max. 100) |
| `discovery_probe_timeout` | number | `8.0` | Timeout der Discovery-ICY-Proben (Sekunden) |
| `discovery_max_concurrent` | integer | `300` | Parallele Discovery-Proben (max. 500) |
| `discovery_random_sample_size` | integer | `10000` | Zufallsstichprobe aus der Community-M3U beim Discovery |
| `acoustid_api_url` | string | `https://api.acoustid.org/v2/lookup` | AcoustID-Lookup-URL |
| `acoustid_requests_per_minute` | integer | `170` | Max. AcoustID-API-Aufrufe pro Minute (Limit: 180 = 3 req/s) |
| `acoustid_min_score` | float | `0.9` | Mindest-AcoustID-Score für behaltene Aufnahmen (0.0–1.0) |
| `acoustid_retry_max_attempts` | integer | `5` | Wiederholungen bei transienten AcoustID-Fehlern |
| `acoustid_retry_base_delay` | float | `30.0` | Start-Wartezeit vor Retry (Sekunden) |
| `acoustid_retry_max_delay` | float | `3600.0` | Maximale Wartezeit zwischen Retries (Sekunden) |
| `max_unchecked_files` | integer | `5000` | Queue-Limit: max. Dateien in `work/unchecked_mp3` (ist die AcoustID-Queue); bei Überschreitung pausieren alle Streams |
| `max_unchecked_bytes` | integer | `10737418240` | Queue-Limit: max. Größe von `work/unchecked_mp3` (10 GB) |
| `log_file_max_bytes` | integer | `5242880` | Max. Größe der Logdatei (5 MB), danach Rotation |
| `log_file_backup_count` | integer | `5` | Anzahl aufbewahrter Logdateien |

### Live-Config (Hot-Reload)

Die App prüft **alle 60 Sekunden**, ob `config.json` geändert wurde.
Erkannte Änderungen werden übernommen – ohne Neustart des Containers.

- `log_level` – wird sofort gesetzt, ohne Neustart
- `max_files_inbox` – neuer Schwellwert, wirkt beim nächsten Backpressure-Check (alle 30 s)
- **Alle übrigen Felder** (u. a. `stream_keywords`, `discovery_enabled`, `request_timeout`,
  `reconnect_*`, `no_icy_disable_after`, `ignore_title_patterns`, `min_file_size_bytes`,
  `min_file_duration_s`, `work_dir`, `destination`, `streams`, `user_agent`,
  `max_concurrent_streams`, `acoustid_*`, `max_unchecked_*`) lösen einen **Neustart der
  Stream-Recorder** aus: Sie werden gestoppt, die Sender neu aufgelöst (inkl. Discovery /
  Preflight) und wieder gestartet.

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
3. **Song-Erkennung**: Bei Titelwechsel wird die vorherige Aufnahme abgeschlossen und eine neue gestartet
4. **Staging**: Die Aufnahme wird als `.part`-Datei in `work/unchecked_mp3/` geschrieben – das ist die AcoustID-Queue (durabel, überlebt Neustarts)
5. **Commit**: Bei Titelwechsel wird die `.part`-Datei atomar zu `.mp3` committet und an die AcoustID-Queue übergeben
6. **Validierung**: Größe, Dauer und MP3-Gültigkeit werden geprüft – zu kleine, zu kurze oder ungültige Dateien werden automatisch gelöscht
7. **AcoustID-Fingerprint**: Chromaprint (`fpcalc`) prüft per AcoustID, ob der Track einem bekannten Song entspricht (Score ≥ `acoustid_min_score`, Standard 0,9). Nur Treffer mit nutzbaren Künstler-/Titel-Metadaten werden behalten, mit ID3-Tags versehen, als `{Künstler} - {Titel}.mp3` benannt und ins Zielverzeichnis verschoben. Aufnahmen ohne ausreichenden Match werden verworfen.

### Stream-Recorder Lebenszyklus

Jeder Stream wird von einem eigenen `StreamRecorder` verwaltet:

```
_run_forever()
  ├─ pause() / resume()           # Backpressure (Zielverzeichnis voll / Queue-Limits)
  ├─ _run_once()
  │   ├─ Playlist auflösen (M3U/PLS → Stream-URL)
  │   ├─ _connect_stream()
  │   │   ├─ HTTP-GET mit Icy-MetaData: 1
  │   │   └─ ICY-Metaint parsen
  │   └─ _stream_with_meta()
  │       ├─ async for chunk: Parser füttern
  │       ├─ TitleChanged → Aufnahme committen (.part → .mp3), neue starten
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
  │   └─ work/stations/custom.m3u laden → falls leer: leere Datei anlegen
  ├─ PlaylistDiscoveryService.load_or_discover()   # nur falls discovery_enabled
  │   ├─ work_stations.m3u (Cache) gültig? → laden + auswählen
  │   ├─ filtered_checked_stations.m3u gültig? → laden + auswählen
  │   ├─ Sonst: Community-M3U von junguler/m3u-radio-music-playlists laden (Zufallsstichprobe)
  │   ├─ Einträge mit ICY-Probe testen
  │   ├─ Ergebnisse in filtered_checked_stations.m3u cachen
  │   └─ Nach Keywords filtern oder zufällig auswählen
  ├─ _apply_stream_limit()
  │   └─ custom.m3u-Stationen priorisieren
  └─ _preflight_check()
      └─ Alle Stationen vor Start auf ICY-Erreichbarkeit prüfen (mit Fortschritts-Log alle 10 %)
```

### Backpressure (volle Verzeichnisse / volle Queue)

Sobald eines der folgenden Limits erreicht ist, **pausieren alle Streams**
(der aktuell laufende Song wird noch fertig geschrieben):

1. Anzahl `.mp3`-Dateien im Zielverzeichnis ≥ `max_files_inbox`
2. Anzahl Dateien in `work/unchecked_mp3` ≥ `max_unchecked_files`
3. Gesamtgröße von `work/unchecked_mp3` ≥ `max_unchecked_bytes`

Alle **30 Sekunden** wird geprüft, ob die Limits wieder unterschritten sind.
**Automatisch weiter** geht es, sobald alle Werte auf ≤ 80 % des jeweiligen Limits gefallen sind.

So wird verhindert, dass bei vollen Verzeichnissen sinnlos Daten geschrieben werden.

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
Der Container startet standardmäßig `radio-ripper`; du kannst Argumente wie `--log-level DEBUG`
anhängen. Liegt `/app/config/config.json` vor, wird es automatisch per `--config` geladen.

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
