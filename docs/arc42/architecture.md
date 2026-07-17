# Radio-Ripper — Architektur-Dokumentation (arc42)

> Version 2.0 · Stand 2026-07-17 · Status: implementiert

## 1. Einführung und Ziele

### 1.1 Aufgabenbeschreibung

Radio-Ripper ist ein produktionsreifer Webradio-Ripper, der mehrere ICY-Metadaten-Streams **parallel und dauerhaft** im Hintergrund aufzeichnet. Er trennt Lieder automatisch anhand der `StreamTitle`-Wechsel, vermeidet Duplikate über eine lokale SQLite-Datenbank, taggt die MP3-Dateien mit ID3v2 und reichert Metadaten optional über die iTunes Search API an (inklusive Cover-Art).

### 1.2 Qualitätsziele

| # | Qualitätsziel | Motivation |
|---|---|---|
| 1 | **Wartbarkeit** | Klare Schichtung (Hexagonal), kleine Module, Testbarkeit |
| 2 | **Betriebssicherheit** | Automatischer Reconnect mit exponentiellem Backoff, Graceful Shutdown |
| 3 | **Datenintegrität** | Nur komplette Lieder werden gespeichert, Duplikate via SQLite dedup |
| 4 | **Erweiterbarkeit** | Services als ABCs, neue Quellen/Tagger/Provider ohne Core-Änderung |
| 5 | **Typsicherheit** | mypy strict, Pydantic-Validierung der Config |

### 1.3 Stakeholder

| Rolle | Interesse |
|---|---|
| Endbenutzer | Einfacher Start (`run.sh`), keine Duplikate, getaggte MP3s |
| Entwickler | Klares Layout, 151 Tests, mypy clean, CI-Grün |
| Operator | Docker, Health-Check, Graceful Shutdown, PID-Datei |

---

## 2. Randbedingungen

| Aspekt | Entscheidung |
|---|---|
| Programmiersprache | Python ≥ 3.11 |
| Build-Tool | uv (hatchling backend) |
| Konfiguration | `config.json` (Pydantic v2 validiert) |
| Single-Entry-Point | `uv run radio-ripper --config config.json` |
| Runtime | Lokal via `run.sh` oder Docker-Container |
| Betriebsart | Long-running Prozess, SIGINT/SIGTERM → Graceful Shutdown |

---

## 3. Kontext und Überblick

### 3.1 System-Kontext

```
                          ┌──────────────┐
                          │   Internet    │
                          │   Webradio    │
                          │   (.m3u/.pls) │
                          └──────┬───────┘
                                 │ HTTP/ICY
                                 ▼
┌─────────────────────────────────────────────┐
│              Radio-Ripper v2                  │
│                                               │
│  ┌─────────┐  ┌───────────┐  ┌────────────┐  │
│  │ Playlist │  │  Stream   │  │ Metadata   │  │
│  │ Resolver │──│  Recorder │──│ Provider   │  │
│  │         │  │ (per Stream)│ │ (iTunes)   │  │
│  └─────────┘  └─────┬─────┘  └────────────┘  │
│                      │                        │
│              ┌───────┼───────┐                │
│              ▼       ▼       ▼                │
│         TrackWriter │ TrackTagger             │
│         TrackRepo   │                         │
│              │       │                        │
│              ▼       ▼                        │
│         ┌─────────────────┐                   │
│         │  Dateisystem     │                   │
│         │  MP3 + songs.db  │                   │
│         └─────────────────┘                   │
└─────────────────────────────────────────────┘
```

### 3.2 Externe Schnittstellen

| Schnittstelle | Protokoll | Zweck |
|---|---|---|
| Webradio-Stream | HTTP/ICY | Audiostream + Metadaten (icy-metaint) |
| Playlist (.m3u/.pls) | HTTP | Stream-URL-Auflösung |
| iTunes Search API | HTTPS | Metadata anreichern (Artist, Album, Cover) |
| Dateisystem | POSIX | MP3 schreiben, songs.db, Cover-Bilder |
| System-Signals | SIGINT, SIGTERM | Graceful Shutdown |

---

## 4. Lösungsstrategie

### 4.1 Architekturstil: Hexagonal / Layered

```
┌───────────────────────────────────────────────┐
│                  CLI / App                     │  ← Entry-Point
├───────────────────────────────────────────────┤
│              Services-Layer                    │
│  StreamRecorder · IcyParser · TrackWriter      │  ← Business-Logic
│  TrackTagger · TrackRepository · Metadata      │
├───────────────────────────────────────────────┤
│               Infra-Layer                      │
│  AsyncHttpClient · Config · Logging · Errors   │  ← Technical
├───────────────────────────────────────────────┤
│               Domain-Layer                     │
│  TrackInfo · SavedTrack · EnrichedInfo         │  ← Pure Models
└───────────────────────────────────────────────┘
```

### 4.2 Schlüsselentscheidungen

| # | Entscheidung | Begründung | ADR |
|---|---|---|---|
| 1 | Async/await statt threading | I/O-bound, leichtere Fehlerbehandlung | [ADR-0001](adrs/0001-async-await-l threading.md) |
| 2 | Service-ABCs (Dependenz Injection) | Testbarkeit, Ersetzbarkeit | [ADR-0002](adrs/0002-service-abcs-di.md) |
| 3 | Pydantic v2 für Config | Validierung, Defaults, Schema | [ADR-0003](adrs/0003-pydantic-v2-config.md) |
| 4 | Nur komplette Songs speichern | Datenqualität > Quantität | [ADR-0004](adrs/0004-complete-songs-only.md) |

---

## 5. Bausteinsicht

### 5.1 Level 1: Top-Level

Siehe Kontextdiagramm (Section 3.1).

### 5.2 Level 2: Module

| Modul | Layer | Verantwortung |
|---|---|---|
| `cli.py` | Entry | Argparse, Signal-Handler, `main()` |
| `app.py` | Entry | `RadioRipperApp`: orchestriert alle Services |
| `infra/config.py` | Infra | Pydantic-Modelle für `Settings`, `StreamConfig` |
| `infra/http.py` | Infra | `AsyncHttpClient` ABC, `HttpxAsyncClient` Impl |
| `infra/errors.py` | Infra | Exception-Hierarchie |
| `infra/logging.py` | Infra | Log-Konfiguration |
| `infra/resilience.py` | Infra | Retry/Backoff-Helper |
| `domain/models.py` | Domain | `TrackInfo`, `SavedTrack`, `EnrichedInfo` |
| `services/icy.py` | Service | `IcyParser` State-Machine (pure) |
| `services/playlist.py` | Service | `.m3u`/`.pls` Resolver |
| `services/storage.py` | Service | `TrackWriter` (temp file → atomic rename) |
| `services/tagging.py` | Service | `TrackTagger` ABC, `ID3Tagger` (mutagen) |
| `services/repository.py` | Service | `TrackRepository` ABC, `SQLiteTrackRepository` |
| `services/metadata.py` | Service | `MetadataProvider` ABC, `ITunesMetadataProvider` |
| `services/stream.py` | Service | `StreamRecorder` (Orchestrierungs-Coroutine) |

### 5.3 Level 3: StreamRecorder (Schlüsselkomponente)

```
┌──────────────────────────────────────────────────────────────┐
│                   StreamRecorder (per Station)                │
│                                                                │
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌───────────┐    │
│  │ http.get │──▶│ IcyParser│──▶│TrackWriter│──▶│TrackTagger│    │
│  │ _stream()│   │ State-M. │   │ (temp→mp3)│   │ (ID3v2)   │    │
│  └─────────┘   └──────────┘   └──────────┘   └───────────┘    │
│                     │                              │          │
│                     ▼                              ▼          │
│               ┌──────────┐                ┌──────────────┐     │
│               │ TrackRepo│                │ MetadataProv.│     │
│               │ (dedup)  │←───────exists──│ (iTunes)     │     │
│               └──────────┘                └──────────────┘     │
└──────────────────────────────────────────────────────────────┘
```

---

## 6. Laufzeitsicht

### 6.1 Happy Path: Song-Aufzeichnung

Siehe: `diagrams/sequence-recording.puml`

1. `StreamRecorder._run_forever()` → `_run_once()()`
2. HTTP-Verbindung zum Stream-URL (via `HttpxAsyncClient`)
3. `IcyParser` konsumiert Bytes → emittiert `AudioChunk` + `TitleChanged`
4. Bei `TitleChanged`: alter Song wird abgeschlossen (TrackWriter.commit → atomic rename)
5. `TrackRepository.exists()` prüft Duplikat
6. Falls neu: `TrackTagger.tag()` schreibt ID3v2
7. `TrackRepository.register()` trägt in SQLite ein
8. Optional async: `MetadataProvider.enrich()` (nicht-blockierend)

### 6.2 Reconnect mit Backoff

```
  Fehler → delay = initial_reconnect_delay
     └─→ sleep(delay, cancellable via stop_event)
         └─→ delay = min(delay * 2, reconnect_max_delay) → retry
```

### 6.3 Graceful Shutdown

```
  SIGINT/SIGTERM → stop_event.set()
     └─→ _run_forever loop break
         └─→ in-flight TrackWriter.discard()  (partial song = wegwerfen)
             └─→ PlaylistResolver.aclose(), TrackRepository.aclose()
```

---

## 7. Verteilungssicht

### 7.1 Deployment-Optionen

| Modus | Beschreibung |
|---|---|
| **Lokal** | `./run.sh` startet `uv run radio-ripper` als Vordergrund-Prozess |
| **Docker** | `docker run` mit gemountetem `config.json`, `recordings/`, `songs.db` |

Siehe: `diagrams/deployment.puml`

---

## 8. Querschnittskonzepte

### 8.1 Fehlerbehandlung

Exception-Hierarchie in `infra/errors.py`:

```
RadioRipperError
├── ConfigurationError
├── StreamConnectionError
├── StreamProtocolError
├── RepositoryError
└── TaggingError
```

Jede Exception ist catch-and-log, niemals silent-fail.

### 8.2 Logging

`logging` mit strukturierter Formatierung, konfiguriert via `infra/logging.py`. Log-Level via Config oder `--log-level` CLI-Override.

### 8.3 Konfiguration

Pydantic-v2-Modelle mit Validierung:
- `Settings` (Top-Level): `output_dir`, `database_path`, `enrich_metadata`, `embed_cover_art`, ...
- `StreamConfig`: `name`, `url`, `output_dir`, ...
- Defaults via `config.json`, überschreibbar via CLI-Args.

### 8.4 Testing

- 151 Pytests, 85% Coverage (Gate: 70%)
- `asyncio_mode = "auto"`, Fake-HTTP via `respx`
- Stream-Tests mit Fake `AsyncHttpClient`, kein Real I/O

---

## 9. Architekturentscheidungen

Siehe: [ADRs](adrs/)

| ADR | Titel | Status |
|---|---|---|
| [0001](adrs/0001-async-await-l threading.md) | Async/await statt threading | Angenommen |
| [0002](adrs/0002-service-abcs-di.md) | Service-ABCs für Dependenz Injection | Angenommen |
| [0003](adrs/0003-pydantic-v2-config.md) | Pydantic v2 für Config-Validierung | Angenommen |
| [0004](adrs/0004-complete-songs-only.md) | Nur komplette Songs speichern | Angenommen |

---

## 10. Qualitätsanforderungen

| Qualitätsmerkmal | Anforderung | Verifikation |
|---|---|---|
| Wartbarkeit | Module < 250 LOC, max 1 Veranwortung | Code-Review |
| Testbarkeit | 151 Tests, Coverage ≥ 70% | `pytest --cov` |
| Typsicherheit | mypy strict, 0 errors | `uv run mypy` |
| Lint | ruff clean, 0 errors | `uv run ruff check` |
| Datenintegrität | Keine partiellen Songs, keine Dupes | Integrationstest |
| Betriebssicherheit | Graceful Shutdown < 30s | manuell, Signal-Test |

---

## 11. Risiken und technische Schulden

| # | Risiko | Mitigation |
|---|---|---|
| 1 | iTunes API kann Rate-Limit | Backoff in MetadataProvider |
| 2 | Stream sendet keine ICY-Metadaten | Detektion, Logs, Stream wird übersprungen |
| 3 | SQLite als Single-Writer | asyncio.Lock + to_thread |
| 4 | Mutagen hat keine Type-Stubs | `# mypy: disable-error-code` in tagging.py |

---

## 12. Glossar

| Begriff | Bedeutung |
|---|---|
| ICY | Metadatenprotokoll für Shoutcast/Icecast-Streams |
| icy-metaint | HTTP-Header: Abstand der Metadaten-Blöcke in Bytes |
| StreamTitle | Feld im ICY-Metadaten-Block (`Artist - Title`) |
| m3u/pls | Playlist-Formate, Stream-URLs enthaltend |
| ID3v2 | Tagging-Standard für MP3-Dateien |
| WAL | SQLite Write-Ahead-Logging für non-blocking reads |
| Graceful Shutdown | Sauberes Beenden: in-flight Songs verwerfen, DB schließen |