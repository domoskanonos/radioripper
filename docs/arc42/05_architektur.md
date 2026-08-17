# 05 — Bausteinansicht

## Modulübersicht

```
src/radio_ripper/
├── workflow.py        # Einstieg & Orchestrierung (CLI, Lifecycle)
├── config.py          # Settings, JSONC-Laden
├── logging_setup.py   # Zentrale Logging-Konfiguration
├── models.py          # Datenklassen (M3uEntry, AcoustidMatch, StreamConfig)
├── m3u.py             # Senderliste laden/parsen
├── icy.py             # ICY-Metadaten-Parser
├── writer.py          # TrackWriter (.part → .mp3, atomar)
├── http_client.py     # Async HTTP-Client + Playlist-Auflösung
├── validation.py      # Größen- & Dauer-Validierung
├── acoustid.py        # AcoustID-Pipeline (Fingerprint, Lookup, Tags, Worker)
└── recorder.py        # StreamRecorder (Aufnahme-Schleife)
```

## Schichten & Abhängigkeiten

```
┌──────────────────────────────────────────────┐
│                  workflow.py                 │  Orchestrierung
├──────────┬──────────┬────────────────────────┤
│ recorder │ acoustid │        m3u             │  Business-Logik
├────┬─────┴────┬─────┴────┬───────────────────┤
│ icy│ writer   │ validation│  http_client      │  Domain/Infrastruktur
└────┴──────────┴──────────┴───────────────────┘
          │
      config.py, models.py, logging_setup.py    │  Basis
```

## Komponenten-Verantwortlichkeiten

| Modul | Verantwortung | Hängt ab von |
|-------|---------------|--------------|
| `workflow` | CLI, Signal-Handling, Lifecycle, ThreadPool, Worker | alles |
| `recorder` | Verbinden, ICY parsen, aufnehmen, validieren, enqueue | icy, writer, validation, http_client, acoustid |
| `acoustid` | fpcalc, Lookup, ID3-Tags, Kollision, Singleton-Worker | models, writer, config |
| `validation` | Größe + Dauer (ffprobe) | config |
| `writer` | TrackWriter (atomarer .part → .mp3 Commit) | — |
| `icy` | ICY-Metadaten-State-Machine | — |
| `http_client` | HTTP-Streaming, Playlist-Auflösung | — |
| `m3u` | Senderliste laden | models, config |

## Concurrency-Modell

- **Recorder:** 1 `asyncio.Task` pro Sender (I/O-gebunden)
- **AcoustID:** 1 Singleton-`asyncio.Task` (sequenziell, FIFO-Queue) — natürliches Rate-Limit
- **ffprobe/fpcalc:** `ThreadPoolExecutor` (Größe = Sender-Anzahl)
- **Kollision:** `threading.Lock` um Prüfung + Verschieben
