# Radio-Ripper

Production-grade Webradio-Ripper in Python: zeichnet mehrere Streams **dauerhaft und parallel** im Hintergrund auf, **trennt Lieder automatisch** anhand der ICY-Metadaten (`StreamTitle`-Wechsel), **vermeidet Duplikate** uber eine lokale SQLite-Datenbank, **taggt** MP3-Dateien mit ID3v2 und reichert sie mit iTunes-Cover-Art an.

## Schnellstart

```bash
uv sync
uv run radio-ripper --config config.json
```

## Features

- Multi-Stream dauerhaft mit parallelen asyncio-Tasks
- ICY-Metadaten-Parsing mit State Machine
- Duplikats-Erkennung via SQLite (WAL-Modus)
- ID3v2-Tagging mit Cover-Art
- Metadaten-Anreicherung via iTunes + CoverArtArchive
- Audio-Fingerprinting via AcoustID / MusicBrainz
- Automatische Radio-Station-Discovery
- Graceful Shutdown via SIGINT/SIGTERM
- Docker-Support
- REST-API (Config, Stations, Library, Ripper-Steuerung)
- Gradio-Web-GUI (optional)
