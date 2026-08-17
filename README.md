# Radio-Ripper – Stream

Dauerhafte parallele Aufnahme von Webradio-Streams mit ICY-Metadaten (Songtitel).
Erkennt und parst ICY-Stream-Metadaten, trennt Aufnahmen an Songgrenzen, validiert
jede Aufnahme (Größe + Dauer) und identifiziert sie anschließend über **AcoustID**.
Erfolgreich identifizierte Songs werden mit ID3-Tags angereichert und in eine
standardmäßige Ordnerstruktur verschoben.

## Features

- **Parallel-Recording** – nimmt die konfigurierte Anzahl Streams gleichzeitig auf
- **ICY-Metadaten** – erkennt Songtitel in Echtzeit, trennt Aufnahmen sauber an Songgrenzen
- **Auto-Reconnect** – bei Verbindungsabbruch automatische Wiederverbindung mit exponentiellem Backoff + Jitter
- **Werbefilter** – überspringt Titel per Regex-Muster (`ignore_title_patterns`)
- **Datei-Validierung** – verwirft zu kurze (< `min_file_duration_s`) und zu kleine (< `min_file_size_bytes`) Dateien
- **AcoustID-Identifikation** – Fingerprint + Lookup; nur Treffer ≥ `acoustid_min_score` (Standard 0.9) werden behalten
- **ID3-Tagging** – Artist, Title, Album, Jahr, Tracknummer, MusicBrainz-IDs, Score
- **Ordnerstruktur** – `Artist/Album/Artist - Title.mp3`
- **Kollisions-Handling** – bei gleichem Dateinamen gewinnt der höhere AcoustID-Score
- **Graceful Shutdown** – stoppt alle Recorder parallel, beendet den AcoustID-Worker und schließt den ThreadPool sauber

## Voraussetzungen

- **Python ≥ 3.11** mit [uv](https://docs.astral.sh/uv/)
- **ffprobe** (Teil von ffmpeg) für die Dauer-Validierung
- **fpcalc** (Chromaprint) für den AcoustID-Fingerprint
- **AcoustID-API-Key** (`ACOUST_ID`)

```bash
# Debian/Ubuntu
sudo apt install ffmpeg libchromaprint-tools
```

## Schnellstart

```bash
# Abhängigkeiten installieren
uv sync

# Config anlegen (optional – ohne Config gelten Defaults)
cp config/config_example.jsonc config/config.jsonc
# → ggf. destination/ und werkverzeichnis anpassen

# Senderliste bearbeiten
# work/stations/custom.m3u im M3U-Format:
#   #EXTINF:-1,Sender Name
#   http://stream.example.de/live.mp3

# Starten (ACOUST_ID ist Pflicht für Identifikation)
ACOUST_ID=<dein_key> uv run radio-ripper -c config/config.jsonc
```

Alternativ mit eingebautem `.env`:

```bash
echo 'ACOUST_ID=<dein_key>' > .env
uv run radio-ripper -c config/config.jsonc
```

## Konfiguration

| Feld | Default | Bedeutung |
|------|---------|-----------|
| `work_dir` | `./work` | Staging: `recordings/`, `stations/` |
| `destination` | `./destination` | Ziel für fertige, getaggte MP3s |
| `log_level` | `INFO` | Log-Level |
| `max_concurrent_streams` | `500` | Maximale gleichzeitige Aufnahmen |
| `user_agent` | `VLC/3.0.18 LibVLC/3.0.18` | HTTP User-Agent |
| `request_timeout` | `30` | HTTP-Timeout (s) |
| `reconnect_base_delay` | `1.0` | Initiale Reconnect-Verzögerung (s) |
| `reconnect_max_delay` | `60.0` | Maximale Reconnect-Verzögerung (s) |
| `no_icy_disable_after` | `10` | Sender ohne ICY deaktivieren nach N Verbindungen |
| `ignore_title_patterns` | `[]` | Regex-Muster für zu überspringende Titel |
| `min_file_size_bytes` | `1572864` | Mindestgröße einer Aufnahme (1.5 MB) |
| `min_file_duration_s` | `90` | Mindestdauer einer Aufnahme (s) |
| `acoustid_min_score` | `0.9` | Mindest-Score für AcoustID-Treffer |

Der AcoustID-API-Key wird aus der Umgebungsvariable `ACOUST_ID` gelesen.

## Ablauf

```
Recorder → TrackWriter (recordings/) → commit → Validierung (Größe + Dauer)
        → AcoustID-Worker (sequenziell)
            → fpcalc (Fingerprint) → Lookup
            → kein Treffer ≥ Score → löschen
            → Treffer → ID3-Tags → destination/Artist/Album/Artist - Title.mp3
```

## Docker

```bash
docker run --rm \
  -e ACOUST_ID=<dein_key> \
  -v "$PWD/config:/app/config:ro" \
  -v "$PWD/work:/app/work" \
  -v "$PWD/destination:/app/destination" \
  domoskanonos/radio-ripper-stream
```

## Entwicklung

```bash
uv sync --group dev

# Lint & Format
uv run ruff check src/radio_ripper/ tests/
uv run ruff format --check src/radio_ripper/ tests/

# Typprüfung
uv run mypy src/radio_ripper/

# Tests mit Abdeckung (Ziel ≥ 90 %)
uv run pytest tests/ --cov=radio_ripper --cov-fail-under=90
```

## Dokumentation

Die Systemdokumentation nach arc42 liegt unter [`docs/arc42/`](docs/arc42/README.md)
(inkl. PlantUML-Diagrammen unter `docs/arc42/diagrams/`).
