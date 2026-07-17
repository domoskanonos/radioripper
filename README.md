# Radio-Ripper

Produktionsreifer Webradio-Ripper in Python: zeichnet mehrere Streams **dauerhaft und parallel** im Hintergrund auf, **trennt Lieder automatisch** anhand der ICY-Metadaten (`StreamTitle`-Wechsel), **vermeidet Duplikate** über eine lokale SQLite-Datenbank, **taggt** MP3-Dateien mit ID3v2 und reichert sie mit iTunes-Cover-Art an.

Gebaut mit [`httpx`](https://www.python-httpx.org/), [`pydantic`](https://docs.pydantic.dev/) und [`mutagen`](https://mutagen.readthedocs.io/), gebündelt via [`uv`](https://docs.astral.sh/uv/).

---

## Features

- **Multi-Stream dauerhaft**: parallele asyncio-Tasks pro Station, autonomer Reconnect mit **Exponential Backoff**.
- **M3U & PLS Parser**: lädt Playlist-URLs und wählt die erste gültige Stream-URL.
- **ICY-Metadata-Parsing**: state machine für saubere Trennung vom Audiostream, Titel-Wechsel erzeugt neuen Song.
- **Duplikats-Erkennung**: SQLite-DB (WAL-Modus) speichert `Interpret - Titel` pro Station.
- **Saubere Dateinamen**: illegale Zeichen bereinigt, `(2)`, `(3)`-Suffixe bei Kollisionen.
- **ID3v2-Tagging**: `TPE1` (Artist), `TIT2` (Title), `APIC` (Cover-Art), `COMM` + `TXXX:RIPPEDBY`.
- **iTunes-Cover-Art-Enrichment**: lädt Album-Art über Search-API (`--no-enrich` deaktivierbar).
- **Mindestgrößen-Schutz**: unvollständige Aufnahmen werden verworfen.
- **Robustes Logging**: Console + rotierendes File.
- **Graceful Shutdown** via `SIGINT` / `SIGTERM`.
- **Gradle-Web-GUI** (optional): 4 Tabs für Senderverwaltung, Config-Edition, Bibliothek und Ripper-Steuerung.

---

## Projektstruktur

```
radioripper/
├── src/radio_ripper/     # Python-Paket
│   ├── api/              # API-Layer (ConfigApi, StationApi, LibraryApi, RipperApi)
│   ├── gui/              # Gradio-Web-GUI (optionales Extra)
│   ├── services/         # Service-ABCs + Stream-, Playlist-, Cover-Service
│   ├── models/           # Pydantic-Modelle (Settings, StreamConfig, …)
│   └── cli.py            # CLI-Entry-Point
├── tests/                # 189 Tests (pytest + pytest-asyncio + respx)
├── docs/
│   ├── arc42/            # Architekturdokumentation
│   └── diagrams/         # PlantUML-Diagramme (Container, Komponenten, …)
├── config.json           # Konfiguration (Beispiel)
├── pyproject.toml        # Build + Dependencies
├── Dockerfile            # Container-Build
├── .github/workflows/    # CI (Matrix 3.11/3.12, Ruff, MyPy, Pytest, Coverage)
└── .pre-commit-config.yaml
```

Zur Laufzeit automatisch erstellt:

```
recordings/
├── ripper.db             # SQLite-Duplikats-DB
├── TopHits/              # Pro Station ein Verzeichnis
│   └── Adele - Hello.mp3
└── ...
radio_ripper.log          # Rotierendes Logfile (5 MB × 5)
```

---

## Voraussetzungen

- **Python ≥ 3.11** (getestet mit 3.12)
- **[uv](https://docs.astral.sh/uv/)** (empfohlen)

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

---

## Setup

```bash
cd /pfad/zum/radioripper
uv sync                           # Core-Dependencies
uv sync --extra gui               # + Gradio-GUI
uv sync --extra dev               # + Entwicklungs-Tools
```

---

## Konfiguration

Alle Einstellungen in `config.json`:

```jsonc
{
  "destination": "./recordings",
  "database":    "./recordings/ripper.db",
  "streams": [
    { "name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u" },
    { "name": "Rock",    "url": "http://rock.radiomonster.fm/listen.m3u" }
  ],
  "request_timeout":         30,
  "read_chunk":              4096,
  "reconnect_base_delay":    1.0,
  "reconnect_max_delay":     60.0,
  "user_agent":              "Radio-Ripper/1.0",
  "overwrite_existing_files": false,
  "min_file_size_bytes":     1024,
  "log_level":               "INFO",
  "log_file":                "./radio_ripper.log"
}
```

---

## Start

### CLI (Vordergrund)

```bash
uv run radio-ripper --config config.json
uv run radio-ripper --config config.json --log-level DEBUG
uv run radio-ripper --no-enrich        # ohne iTunes-Cover-Art
```

### GUI (Web-Interface)

```bash
uv sync --extra gui
uv run radio-ripper-gui --config config.json
```

Öffnet **`http://localhost:7860`** im Browser. Vier Tabs:

1. **Sender** — Stationen anzeigen/hinzufügen/löschen
2. **Konfiguration** — Einstellungen live bearbeiten
3. **Bibliothek** — aufgenommene Songs durchsuchen
4. **Ripper-Steuerung** — Start/Stopp + Live-Status

### run.sh (Legacy)

```bash
./run.sh
```

### systemd-Service

```ini
[Unit]
Description=Radio-Ripper
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/radioripper
ExecStart=/usr/bin/env uv run radio-ripper --config /opt/radioripper/config.json
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now radio-ripper
```

### Docker

```bash
docker build -t radio-ripper .
docker run -v ./config.json:/app/config.json -v ./recordings:/app/recordings radio-ripper
```

---

## CLI-Optionen

```
usage: radio-ripper [-h] [-c CONFIG] [--log-level LEVEL] [--no-enrich] [--version]

  -c, --config CONFIG       Pfad zur config.json
  --log-level LEVEL         überschreibt config-log-level
  --no-enrich               iTunes-Cover-Art deaktivieren
  --version                 Version anzeigen
  -h, --help                Hilfe anzeigen
```

Wird `--config` weggelassen: `./config.json` → `~/.config/radio_ripper/config.json` → `/etc/radio_ripper/config.json`.

---

## Stoppen

- **Vordergrund / GUI**: `Strg+C` — Graceful Shutdown, alle Dateien finalisiert & getaggt.
- **systemd**: `sudo systemctl stop radio-ripper`

---

## Funktionsweise (kurz)

1. Pro Station ein **asyncio-Task**: lädt Playlist → wählt Stream-URL → HTTP-GET mit `Icy-MetaData: 1`.
2. **IcyParser-State-Machine** liest `icy-metaint`-Blöcke, extrahiert `StreamTitle`.
3. Bei Titel-Wechsel: alte Datei finalisieren (ID3 + Cover + DB), neue öffnen.
4. Duplikats-Prüfung via SQLite — bekannte Songs werden verworfen.
5. Bei Verbindungsabbruch: Reconnect mit **Exponential Backoff** (1–60s).

---

## Entwicklung

```bash
uv sync --extra dev
uv run ruff check src/ tests/
uv run mypy src/radio_ripper/
uv run pytest -v --cov
uv run pytest --cov --cov-report=html    # Coverage-Report als HTML
```

CI (GitHub Actions) läuft bei jedem Push: Ruff → MyPy → Pytest (3.11 + 3.12) → Coverage ≥ 70%.

---

## Lizenz

Siehe [LICENSE](LICENSE).
