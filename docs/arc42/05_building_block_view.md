# 5. Bausteinsicht

## 5.1 Level 1: Top-Level

Siehe Kontextdiagramm (Abschnitt 3).

## 5.2 Level 2: Module

| Modul | Layer | Verantwortung |
|---|---|---|
| `cli.py` | Entry | Argparse, Signal-Handler, `main()` |
| `app.py` | Entry | `RadioRipperApp`: orchestriert alle Services |
| `infra/config.py` | Infra | Pydantic-Modelle fur `Settings`, `StreamConfig` |
| `infra/http.py` | Infra | `AsyncHttpClient` ABC, `HttpxAsyncClient` Impl |
| `infra/errors.py` | Infra | Exception-Hierarchie |
| `infra/logging.py` | Infra | Log-Konfiguration |
| `infra/resilience.py` | Infra | Retry/Backoff-Helper |
| `domain/models.py` | Domain | `TrackInfo`, `SavedTrack`, `EnrichedInfo` |
| `services/icy.py` | Service | `IcyParser` State-Machine (pure) |
| `services/playlist.py` | Service | `.m3u`/`.pls` Resolver |
| `services/storage.py` | Service | `TrackWriter` (temp file -> atomic rename) |
| `services/tagging.py` | Service | `TrackTagger` ABC, `ID3Tagger` (mutagen) |
| `services/repository.py` | Service | `TrackRepository` ABC, `SQLiteTrackRepository` |
| `services/metadata.py` | Service | `MetadataProvider` ABC, `ITunesMetadataProvider` |
| `services/stream.py` | Service | `StreamRecorder` (Orchestrierungs-Coroutine) |
| `api/config_api.py` | API | `ConfigApi`: Config laden/speichern/editieren |
| `api/station_api.py` | API | `StationApi`: Stationen CRUD |
| `api/library_api.py` | API | `LibraryApi`: Songs-Bibliothek durchsuchen |
| `api/ripper_api.py` | API | `RipperApi`: Ripper in bg-Thread starten/stoppen |
| `gui/gui.py` | GUI | `build_app()`: Gradio-Blocks (4 Tabs), `main()`: Entry-Point |

## 5.3 Level 3: StreamRecorder (Schlusselkomponente)

```
+--------------------------------------------------------------+
|                   StreamRecorder (per Station)                |
|                                                                |
|  +---------+   +----------+   +----------+   +-----------+    |
|  | http.get |-->| IcyParser|-->|TrackWriter|-->|TrackTagger|    |
|  | _stream()|   | State-M. |   | (temp->mp3)|   | (ID3v2)   |    |
|  +---------+   +----------+   +----------+   +-----------+    |
|                     |                              |          |
|                     v                              v          |
|               +----------+                +--------------+     |
|               | TrackRepo|                | MetadataProv.|     |
|               | (dedup)  |<-------exists--| (iTunes)     |     |
|               +----------+                +--------------+     |
+--------------------------------------------------------------+
```
