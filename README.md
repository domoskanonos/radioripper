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

#### Image von Docker Hub beziehen (empfohlen)

```bash
docker pull domoskanonos/radioripper:latest
```

#### Schnellstart (Headless-CLI)

```bash
# 1. Config vorbereiten
cp config.example.json ./config.json
#   → Pfade in config.json anpassen (destination, database, streams)

# 2. Container starten
docker run -d --name radio-ripper --restart unless-stopped \
  -v "$PWD/config.json:/app/config.json:ro" \
  -v "$PWD/recordings:/app/recordings" \
  domoskanonos/radioripper:latest

# Logs verfolgen
docker logs -f radio-ripper

# Stoppen
docker stop radio-ripper
```

#### Volumes

| Mount | Zweck | Beispiel |
|---|---|---|
| `config.json:ro` | **Read-only**-Konfiguration (Stationen, Pfade, Log-Level) | `-v ./config.json:/app/config.json:ro` |
| `recordings/` | Aufgenommene MP3-Dateien + SQLite-Datenbank | `-v ./recordings:/app/recordings` |

> **Hinweis:** Die SQLite-Datenbank (`ripper.db`) liegt standardmäßig im Recording-Pfad. Wenn du sie separat mounten möchtest, setze `"database": "/app/recordings/ripper.db"` in der config.json.

#### docker compose (empfohlen)

```yaml
# docker-compose.yml
services:
  radio-ripper:
    image: domoskanonos/radioripper:latest
    container_name: radio-ripper
    restart: unless-stopped
    volumes:
      - ./config.json:/app/config.json:ro
      - ./recordings:/app/recordings
```

```bash
# Starten
docker compose up -d

# Logs
docker compose logs -f

# Stoppen
docker compose down
```

#### Selbst bauen

#### Selbst bauen

```bash
git clone https://github.com/domoskanonos/radioripper.git
cd radioripper
docker build -t radio-ripper:latest .
```

#### Umgebungsvariablen

Das Image unterstützt keine nativen ENV-Overrides – alle Einstellungen steuert die `config.json`.  
Die folgenden CLI-Argumente können jedoch über den Docker-`entrypoint` gesetzt werden:

| Argument | Wirkung |
|---|---|
| `--log-level DEBUG` | Überschreibt Log-Level aus config |
| `--no-enrich` | Deaktiviert iTunes-Cover-Art |
| `--version` | Zeigt Version an |

```bash
# Beispiel: DEBUG-Logging im Container
docker run --rm \
  -v "$PWD/config.json:/app/config.json:ro" \
  domoskanonos/radioripper:latest \
  --log-level DEBUG
```

#### Graceful Shutdown

```bash
docker stop radio-ripper          # sendet SIGTERM → Graceful Shutdown
docker compose down               # für compose-Setup
```

Der Container fängt `SIGTERM` ab, finalisiert alle laufenden Aufnahmen, taggt die MP3s und schreibt die Datenbank sauber weg.

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

---

## ⚖️ Rechtlicher Hinweis / Legal Disclaimer

**DE:** Diese Software ist ausschließlich für **private, nicht-kommerzielle Test- und Bildungszwecke** bestimmt. Sie zeichnet öffentlich zugängliche Internet-Radiostreams auf — vergleichbar mit dem zeitversetzten Hören (Time-Shifting) eines Radios oder DVR-Geräts.

- **Du bist allein verantwortlich** dafür, dass die Nutzung dieser Software mit den Urheberrechtsgesetzen und Nutzungsbedingungen der von dir aufgerufenen Streams in deinem Land vereinbar ist.
- **Gib keine Mitschnitte weiter, veröffentliche sie nicht und vermarkte sie nicht.**
- **Umgehe kein DRM und greife nicht auf zugriffsgeschützte Inhalte zu.**
- Die Autoren und Mitwirkenden von Radio-Ripper übernehmen **keine Haftung** für die missbräuchliche Verwendung dieser Software.

---

**EN:** This software is intended for **private, non-commercial, educational and personal testing purposes only**. It records publicly available internet radio streams — analogous to time-shifting with a radio or DVR device.

- **You are solely responsible** for ensuring that your use complies with applicable copyright laws and the terms of service of any streams you access in your jurisdiction.
- **Do not** distribute, publish, or monetize recordings made with this tool.
- **Do not** bypass DRM or access restricted content.
- The authors and contributors of Radio-Ripper assume **no liability** for any misuse of this software.
