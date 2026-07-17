# Radio-Ripper

Produktionsreifer Webradio-Ripper in Python: zeichnet mehrere Streams **dauerhaft und parallel** im Hintergrund auf, **trennt Lieder automatisch anhand der ICY-Metadaten** (`StreamTitle`-Wechsel), **vermeidet Duplikate** über eine lokale SQLite-Datenbank und **taggt** die resultierenden MP3-Dateien mit ID3v2 (`mutagen`).

Gebaut mit [`requests`](https://docs.python-requests.org/) und [`mutagen`](https://mutagen.readthedocs.io/), gebündelt via [`uv`](https://docs.astral.sh/uv/).

---

## Features

- **Multi-Stream dauerhaft**: parallele Background-Threads pro Station, autonomer Reconnect mit **Exponential Backoff**.
- **M3U & PLS Parser**: lädt Playlist-URLs (z. B. `radiomonster.fm/listen.m3u`) und wählt die erste Stream-URL.
- **ICY-Metadata-Parsing**: sendet `Icy-MetaData: 1`, liest `icy-metaint`, zerlegt den Bytestrom sauber und schneidet beim Titel-Wechsel.
- **Duplikats-Erkennung**: SQLite-DB (WAL-Modus, thread-safe) speichert `Interpret - Titel` pro Station. Bereits bekannte Songs werden verworfen.
- **Saubere Dateinamen**: illegale Zeichen bereinigt, Format `Interpret - Titel.mp3`, automatische `(2)`, `(3)`-Suffixe bei Namenskollisionen.
- **ID3v2-Tagging**: schreibt `TPE1` (Artist), `TIT2` (Title), `COMM` ("Recorded via Radio-Ripper") und `TXXX:RIPPEDBY` (Station + Playlist-URL).
- **Mindestgrößen-Schutz**: Dateien unter `min_file_size_bytes` werden verworfen (kein Song-Junk).
- **Robustes Logging** via `logging`-Modul (Console + rotierendes File `radio_ripper.log`).
- **Graceful Shutdown** via `SIGINT` / `SIGTERM` (kümmert sich um offene Files & DB).

---

## Projektstruktur

```
radioripper/
├── radio_ripper.py      # Hauptprogramm (Single-File Module)
├── config.json          # Konfiguration (Streams, Pfade, Parameter)
├── run.sh               # Start-Skript (uv sync + Vordergrund + Live-Log)
├── pyproject.toml       # uv / hatchling Build-Konfiguration & Metadaten
├── .python-version      # Pinning: 3.12
├── requirements.txt     # Legacy-Requirements (für plain pip)
├── README.md            # Diese Datei
└── LICENSE
```

Zur Laufzeit werden automatisch erstellt:

```
recordings/
├── ripper.db            # SQLite-Duplikats-DB
├── TopHits/             # Pro Station ein Verzeichnis
│   └── Adele - Hello.mp3
├── Rock/
└── ...
radio_ripper.log         # Rotierendes Logfile (5 MB x 5)
```

---

## Voraussetzungen

- **Python ≥ 3.11** (getestet mit 3.12)
- **[uv](https://docs.astral.sh/uv/)** (empfohlen) — installieren:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  Alternativ über `pip` – dann ohne `uv` (siehe [Start ohne uv](#start-ohne-uv)).

---

## Setup

```bash
cd /pfad/zum/radioripper
uv sync          # venv anlegen + Dependencies installieren
```

`uv sync` erzeugt automatisch ein `.venv`-Verzeichnis und installiert `requests` + `mutagen` aus `pyproject.toml`.

---

## Konfiguration

Alle Einstellungen liegen in `config.json`:

```jsonc
{
  "destination": "./recordings",            // Zielordner (relativ oder absolut)
  "database":    "./recordings/ripper.db",   // SQLite-Duplikats-DB
  "streams": [
    { "name": "TopHits",    "url": "http://tophits.radiomonster.fm/listen.m3u" },
    { "name": "Rock",       "url": "http://rock.radiomonster.fm/listen.m3u" }
    // ... weitere Stationen
  ],
  "request_timeout":         30,            // HTTP-Connect-Timeout (s)
  "read_chunk":              4096,          // Lese-Blockgröße (bytes)
  "reconnect_base_delay":    1.0,           // Start-Backoff (s)
  "reconnect_max_delay":     60.0,          // Backoff-Cap (s)
  "user_agent":              "Radio-Ripper/1.0",
  "overwrite_existing_files": false,        // bestehende Dateien überschreiben?
  "min_file_size_bytes":     1024,          // Mindestgröße, sonst verwerfen
  "log_level":               "INFO",        // DEBUG|INFO|WARNING|ERROR|CRITICAL
  "log_file":                "./radio_ripper.log"
}
```

 stationsnamen werden als Unterverzeichnisnamen verwendet — bitte lowercase, ohne Slashes.

---

## Start

### Komfortabel via `run.sh` (empfohlen)

Das Skript übernimmt alles: prüft `uv`, führt `uv sync` aus, startet den Ripper
im **Vordergrund mit Live-Log auf der Konsole** und leitet `Strg+C` sauber an
den Python-Prozess weiter (Graceful Shutdown). Zusätzlich gibt es einen
Doppelstart-Schutz via `radio_ripper.pid`-Lockfile.

```bash
./run.sh                              # Standard mit ./config.json
CONFIG=/pfad/zur/cfg.json ./run.sh    # andere Config-Datei
Strg+C                                # beenden -> Graceful Shutdown
```

Features von `run.sh`:
- Pre-Checks (`uv` vorhanden? `config.json` vorhanden?)
- Doppelstart-Schutz über PID-Lockfile (`radio_ripper.pid`)
- `uv sync` vor jedem Start (idempotent)
- Signal-Forwarding: `SIGINT` / `SIGTERM` -> Python -> offene MP3-Files werden
  finalisiert & getaggt, SQLite-DB sauber geschlossen
- Exit-Code des Python-Prozesses wird weitergereicht

### Mit `uv` direkt

```bash
# Vordergrund (Log auf Console):
uv run radio-ripper --config config.json

# alternativ direkter Python-Aufruf:
uv run python radio_ripper.py --config config.json

# Mit Debug-Log:
uv run radio-ripper --log-level DEBUG
```

### Im Hintergrund (systemd-Service)

`/etc/systemd/system/radio-ripper.service`:

```ini
[Unit]
Description=Radio-Ripper Webradio Recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=radio
WorkingDirectory=/opt/radioripper
ExecStart=/usr/bin/env uv run radio-ripper --config /opt/radioripper/config.json
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

Aktivieren:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now radio-ripper
journalctl -u radio-ripper -f      # live verfolgen
```

### Start ohne `uv` (plain pip)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python radio_ripper.py --config config.json
```

---

## CLI-Optionen

```
usage: radio-ripper [-h] [-c CONFIG] [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--version]

Options:
  -c, --config CONFIG       Pfad zur config.json
  --log-level LEVEL         überschreibt log_level aus der config
  --version                 Version anzeigen
  -h, --help                Hilfe anzeigen
```

Wird `--config` weggelassen, durchsucht das Programm:

1. `./config.json`
2. `~/.config/radio_ripper/config.json`
3. `/etc/radio_ripper/config.json`

---

## Stoppen

- **Vordergrund:** `Strg+C` (SIGINT) — alle Recorder-Threads werden sauber geschlossen, offene MP3-Files finalisiert & getaggt, DB geschlossen.
- **systemd:** `sudo systemctl stop radio-ripper` (sendet SIGTERM — gleicher Graceful-Shutdown-Pfad).

---

## Funktionsweise (kurz)

1. Für jede Station wird ein **Thread** gestartet, der die Playlist-URL lädt und die erste gültige Stream-URL wählt.
2. Der HTTP-Request erhält den Header `Icy-MetaData: 1`. Antworten enthalten `icy-metaint` (z. B. `16000`), das verlängt den Metadaten-Block im Stream.
3. Der Bytestrom wird gelesen: nach jedem `metaint`-Block folgt 1 Byte Längeninfo + `Länge * 16` Byte Metadaten (utf-8), die `StreamTitle='Artist - Title';` enthält.
4. Sobald `StreamTitle` sich ändert:
   - Aktuelle Datei wird geschlossen (ID3-Tags geschrieben → DB-Eintrag).
   - Duplikats-Check gegen die SQLite-DB pro Station.
   - Ist der Titel bekannt ⇒ Bytes verwerfen, keine Datei anlegen.
   - Sonst: neue Datei öffnen (`Interpret - Titel.mp3`) und weiter streamen.
5. Bei Verbindungsabbruch: Reconnect mit Exponential Backoff (1s → 2 → 4 → … → max 60s).

---

## Entwicklung

```bash
uv sync --extra dev     # inkl. ruff + pyright
uv run ruff check radio_ripper.py
uv run ruff format radio_ripper.py
uv run pyright radio_ripper.py
```

---

## Lizenz

Siehe [LICENSE](LICENSE).