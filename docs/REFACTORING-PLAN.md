# Refactoring-Architekturplan — `radio_ripper`

> Stand: 2026-07-17 · Autor: Architect-Role · Status: Entwurf, durch Code-Agent umsetzbar.

---

## 1. Auftrags- & Rahmenklärung

 keeps existing behaviour intact:
- Multi-Stream dauerhaftes Rippen via `.m3u` / `.pls`
- ICY-Metadaten-Trennung an `StreamTitle`-Wechseln
- SQLite Duplikats-DB pro Station
- ID3v2-Tagging (`mutagen`)
- iTunes-Enrichment & Cover-APIC
- Nur **komplette** Lieder speichern
- `uv`-Build, `run.sh`-Helper

---

## 2. Ist-Zustand & Schwächenmatrix

### 2.1 Ist-Stand

| Aspekt | Zustand |
|--|--|
| Struktur | Monolith `radio_ripper.py` (1214 LOC, 1 Datei, 7 Top-Level-Klassen) |
| Concurrency | `requests` (sync) + `threading.Thread` pro Stream + `ThreadPoolExecutor` für Enrichment |
| Persistenz | `sqlite3` roh, Wrapper `DupDB` mit eigenem Lock |
| Validation | Plain `dataclass`, manuelle Cast im `Config.load()` |
| Testabdeckung | **0 %** |
| CI/CD | nicht vorhanden |
| Tooling | Ruff (lint+format), Pyright (basic), kein pre-commit, kein mypy |
| Doku | README nur, keine arc42, kein PlantUML, keine ADRs |
| Containerisierung | keine |

### 2.2 Schwächenmatrix

| # | Kategorie | Befund | Auswirkung | Maßnahme |
|---|---|---|---|---|
| 1 | **SoC** | `StreamRecorder` (≈400 LOC) vereint HTTP, Icy-Parser, File-IO, ID3-Tagging, DB-Register, Enrichment-Orchestrierung | Nicht testbar, schwer erweiterbar | Aufspalten in `icy.IcyParser`, `storage.TrackWriter`, `tagging.ID3Tagger`, `services.StreamRecorder` |
| 2 | **Abstraktion** | `MetadataFetcher` hartcodiert iTunes; `PlaylistResolver`, `TrackRepository` keine Interfaces | Provider nicht austauschbar | ABCs für Metadata, Playlist, Repository, Tagger, HttpClient |
| 3 | **Concurrency** | `requests` + Threads + ThreadPoolExecutor | Memory-overhead pro Stream, keine saubere Cancellation, Blockierungen im GIL für JSON-Parse | Migration auf `httpx.AsyncClient` + `asyncio.Task` pro Stream, single Event-Loop |
| 4 | **Validation** | `Config.load` casted int/float/path manuell, keine Range-Checks, URLs unvalidiert | Konfigurationsfehler erst zur Laufzeit | Pydantic v2 `BaseSettings` mit Feld-Validatoren |
| 5 | **Error-Handling** | `except Exception` reaktiv, ad-hoc in jeder Methode | Keine kohärente Resilienz-Strategie; Retry ist Custom-While-Loop | Eigene Fehlerhierarchie `errors.py` + `resilience.retry_async` Decorator |
| 6 | **Logging** | Rotating-file + Console, OK | -- | Beibehalten, zentral in `infra/logging.py` |
| 7 | **Testing** | 0 | Höchstes Risiko | Pytest + pytest-asyncio + respx + golden MP3; Spiegelung in `tests/`-Struktur; Coverage ≥ 85 % |
| 8 | **Bugs** | (a) `bytes_until_meta` kann negativ werden, wenn `take` nach Clamping immer noch zu groß; (b) `_stream_no_meta` schreibt untagged Dump ohne ID3 *und* ohne Begrenzung; (c) `current_size` wird immer hochgezählt aber nie gelesen (dead state); (d) `requirements.txt` stale gegenüber `pyproject.toml`; (e) Phase-1-Join-State-Maschine ohne Test | Datenverlust beim Join, kaputte Fallback-Dateien, Duplikate möglich | IcyParser extract + Property-Tests (Hypothesis), `requirements.txt` aus generated source entfernen |
| 9 | **Duplikate** | `_close_current` vs `_safe_close_current` historisch; `current_size` redundant; setzt `stream_title` 3× (DB-Register dupliziert mit Varianten); `_stream_no_meta` schreibt nicht durch Tagger | DRY-Verletzung, 2 Code-Pfade fürs Tagging | Single `_close_current` Logik, alle Songs über `TrackWriter` + `ID3Tagger` |
| 10 | **Inline-Doku** | vorhanden aber Parser-Logik nicht diagrammiert | Kein Onboarding-Contxt | Pydoc + PlantUML-State-Diagramm in docs/ |
| 11 | **Unbenutzt** | `requirements.txt`, `Iterable`-Import (bereits entfernt), `current_size`, `first_title_seen`-Redundanz (information schon in `current_title`) mit unpassender Initialisierung | Wartungs-Ballast | Löschen / vereinheitlichen |
| 12 | **Probleme gleichzeitig** | ripper + tagging + db + metadata + resilience in einem Projekt-Job | Erweiterungen boomen; SRP verletzt | Layered: cli / app / services / domain / infra |

---

## 3. Ziel-Architektur

### 3.1 Layered Hexagon (Ports & Adapters)

```
                 ┌─────────────────────────────┐
                 │       CLI / Entry           │  radio_ripper/cli.py
                 │  argparse, asyncio.run      │
                 └───────────────┬─────────────┘
                                 │
                 ┌───────────────▼─────────────┐
                 │        Application          │  radio_ripper/app.py
                 │  RadioRipperApp: orchestriert│  – start/stop
                 └───────────────┬─────────────┘
                                 │
   ┌─────────────────────────────▼────────────────────────────────┐
   │                      Services (Use-Cases)                     │
   │  stream.StreamRecorder   playlist.PlaylistResolver           │
   │  icy.IcyParser           metadata.MetadataProvider           │
   │  tagging.ID3Tagger       repository.TrackRepository          │
   │  storage.TrackWriter                                        │
   └───┬───────────────────────┬─────────────────────┬──────────┬─┘
       │ (Port ABCs)           │ (Port ABCs)         │ (Ports)  │
   ┌───▼────┐ ┌─────────────┐ ┌▼──────────────┐ ┌▼────────▼─────┐
   │ Domain │ │ Infra/HTTP  │ │ Infra/Persist │ │ Infra/Tagging │
   │ models │ │ httpx-Async │ │ SQLite Repo   │ │ mutagen       │
   └────────┘ └─────────────┘ └───────────────┘ └───────────────┘
```

### 3.2 Modulplan (src-Layout unter `src/radio_ripper/`)

```
radioripper/
├── pyproject.toml
├── uv.lock
├── config.json
├── run.sh
├── radio_ripper.py            # BWC-Dünnschicht: ruft radio_ripper.cli:main
├── src/
│   └── radio_ripper/
│       ├── __init__.py
│       ├── __main__.py         # python -m radio_ripper
│       ├── cli.py              # argparse, signal handling, asyncio.run
│       ├── app.py              # RadioRipperApp (orchestration)
│       ├── domain/
│       │   ├── __init__.py
│       │   └── models.py       # Pydantic-Modelle: StreamConfig, Settings,
│       │                        #   Track, EnrichedInfo
│       ├── services/
│       │   ├── __init__.py
│       │   ├── icy.py           # IcyParser State-Machine (reiner Pure-Python)
│       │   ├── stream.py        # StreamRecorder (asyncio)
│       │   ├── playlist.py     # PlaylistResolver ABC +
│       │   │                    #   HttpPlaylistResolver, StaticPlaylistResolver
│       │   ├── metadata.py      # MetadataProvider ABC,
│       │   │                    #   ITunesMetadataProvider, NullMetadataProvider
│       │   ├── tagging.py       # Tagger ABC, ID3Tagger
│       │   ├── repository.py    # TrackRepository ABC, SQLiteTrackRepository
│       │   └── storage.py       # TrackWriter (Datei-IO, atomicity)
│       ├── infra/
│       │   ├── __init__.py
│       │   ├── config.py        # load_settings(path) -> Settings
│       │   ├── logging.py       # configure_logging()
│       │   ├── http.py           # AsyncHttpClient ABC + HttpxClient (respx-friendly)
│       │   ├── resilience.py    # retry_async decorator
│       │   └── errors.py        # Fehlerhierarchie
│       └── _logging_adapter.py  # Python-logging bridge for asyncio
├── tests/
│   ├── conftest.py             # gemeinsame Fixtures
│   ├── domain/
│   │   ├── test_models.py
│   ├── services/
│   │   ├── test_icy.py          # golden byte-stream fixtures
│   │   ├── test_stream.py       # respx + asyncio Mock-Stream
│   │   ├── test_playlist.py
│   │   ├── test_metadata.py     # respx iTunes-API
│   │   ├── test_tagging.py      # golden MP3 checks
│   │   ├── test_repository.py    # in-memory sqlite
│   │   └── test_storage.py
│   ├── infra/
│   │   ├── test_config.py
│   │   ├── test_http.py
│   │   ├── test_logging.py
│   │   ├── test_resilience.py
│   │   └── test_errors.py
│   └── test_cli.py              # argparse smoke-tests, end-to-end
├── docs/
│   ├── book.toml                # mdBook
│   ├── arc42/
│   │   ├── 01_introduction.md
│   │   ├── 02_context.md
│   │   ├── 03_constraints.md
│   │   ├── 04_strategy.md
│   │   ├── 05_building-blocks.md
│   │   ├── 06_runtime.md
│   │   ├── 07_deployment.md
│   │   ├── 08_concepts.md
│   │   ├── 09_architecture-decisions.md
│   │   ├── 10_quality.md
│   │   ├── 11_risks.md
│   │   └── 12_glossary.md
│   ├── diagrams/
│   │   ├── container.puml
│   │   ├── components.puml
│   │   ├── icy_state.puml
│   │   ├── sequence_record_song.puml
│   │   └── deployment.puml
│   └── adrs/
│       ├── 0001-async-migration.md
│       ├── 0002-pydantic-settings.md
│       ├── 0003-ports-and-adapters.md
│       └── 0004-sqlite-repository.md
├── .github/workflows/ci.yml       # matrix 3.11/3.12: ruff, mypy, pytest
├── .pre-commit-config.yaml         # ruff+format, mypy, eof-sort
├── Dockerfile                       # multi-stage, non-root
├── .dockerignore
└── README.md
```

### 3.3 Auswahl begründet

| Entscheidung | Begründung |
|--|--|
| `src/`-Layout + installierbares Package | Vermeidet versehentliches "import * .py"-Bug, mypy/pytest sauber |
| `httpx.AsyncClient` statt `requests` | Native Async-Cancellation, respx-mockbar, ein einziges Client-Objekt pro Stream |
| `asyncio.Task` pro Stream statt `threading.Thread` | Single-Loop, skalierbar, sauberes SIGINT/SIGTERM via `loop.add_signal_handler` |
| Pydantic v2 für Config | Validierung, JSON-schema, error messages |
| ABCs für externe Provider | Testbarkeit + künftige Provider austauschbar (MusicBrainz, Last.FM) |
| Eigene Fehlerhierarchie | zentrale Resilienz, `retry_async` für Provider |
| pytest + pytest-asyncio + respx | Standard-Stack für async HTTP-Tests in Python >= 3.11 |
| arc62 + mdBook + PlantUML | Vom Architect gewünscht, generierbar |
| BWC-Shim `radio_ripper.py` | Bestehende Doku/`run.sh`/`pyproject scripts` weiterhin nutzbar |

### 3.4 Datenmodell (Pydantic)

```python
class StreamConfig(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\- ]+$")
    url: HttpUrl

class Settings(BaseSettings):
    destination: Path = Path("./recordings")
    database: Path = Path("./recordings/ripper.db")
    streams: list[StreamConfig]
    request_timeout: float = Field(default=30, ge=1)
    read_chunk: int = Field(default=4096, ge=64, le=65536)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1)
    user_agent: str = "Radio-Ripper/2.0"
    overwrite_existing_files: bool = False
    min_file_size_bytes: int = Field(default=1024, ge=0)
    log_level: str = "INFO"
    log_file: Path | None = None
    enrich_metadata: bool = True
    embed_cover_art: bool = True
    enrichment_workers: int = Field(default=4, ge=1, le=32)
    metadata_timeout: float = Field(default=8.0, ge=0.5)
    cover_timeout: float = Field(default=15.0, ge=0.5)

    model_config = SettingsConfigDict(env_prefix="RADIO_RIPPER_", env_nested_delimiter="__")
```

### 3.5 ICY-Parser (zustandsbasiert)

Extrahiert als pure-Funktion-Klasse `IcyParser` mit definierten States (`WAIT_AUDIO`, `READ_META_LEN`, `READ_META`). Testbar ohne HTTP & Threads (golden byte-streams).

```
[WAIT_AUDIO] -- bytes_until_meta-> 0 -> [READ_META_LEN]
[READ_META_LEN] -> read 1 byte -> meta_len = b*16 -> [READ_META]
[READ_META] -> consume meta_len bytes -> parse StreamTitle -> [WAIT_AUDIO]
```

### 3.6 Fehlerhierarchie (`infra/errors.py`)

```
RadioRipperError
├── ConfigurationError
├── StreamError
│   ├── StreamConnectionError
│   ├── StreamProtocolError
│   └── StreamInterruptedError
├── MetadataProviderError
├── TaggingError
└── RepositoryError
```

### 3.7 Testing-Strategie

- **Unit**: domain/services/infra per Modul; pure-Funktionen pytest-default.
- **HTTP-Mocks** via `respx` (httpx- mocker) — kein echtes Netz in CI.
- **Async** via `pytest-asyncio(mode="auto")`.
- **Golden MP3** in `tests/fixtures/`, vergleiche ID3-Frames per `mutagen`.
- **IcyParser**: parametrierte Byte-Stream-Tests (Hypothesis optional/optional).
- **Coverage-Ziel**: ≥ 85 % in src/radio_ripper.
- **Spiegelung**: Tests liegen unter `tests/<package>/test_<module>.py`.

### 3.8 Tooling

| Tool | Zweck |
|--|--|
| ruff | Lint + Format (ersetzt isort/flake8) |
| mypy | Statische Typ-Checks (strict subset) |
| pytest + cov | Tests + Coverage |
| pytest-asyncio | Async-Tests |
| respx | httpx-Mocking |
| pre-commit | Hooks: ruff, ruff-format, mypy, eof-fixer, trailing-whitespace |
| GitHub Actions | Matrix 3.11/3.12, Lint, Type, Tests, Coverage → Codecov |
| Docker | Multi-Stage: builder → runtime, slim base, non-root user |

### 3.9 Dokumentation

`docs/` als mdBook mit `mdbook-plantuml`. arc62 nach Vorlage, PlantUML-Diagramme für Container, Komponenten, Icy-State, Sequence, Deployment. ADRs under `docs/adrs/NUMM-title.md` (Lightweight ADR).

---

## 4. Migrations-Plan (Phasen)

| Phase | Inhalt | Ergebnis |
|--|--|--|
| **1** | `pyproject.toml` aufstellen (Deps: Pydantic, httpx, mutagen, pytest, ruff, mypy, respx, pytest-asyncio); `src/`-Layout + Hatch-Konfig; `radio_ripper.py` BWC-Shim | Läuft noch wie bisher |
| **2** | `infra/` Module: `errors.py`, `logging.py`, `config.py` (Pydantic), `http.py`, `resilience.py`; `domain/models.py` | Settings + Models testbar |
| **3** | Services-Layer ABCs + Default-Implementierungen (`playlist`, `metadata`, `tagging`, `repository`, `storage`, `icy`) | Alle Services isoliert testbar |
| **4** | `services/stream.py` asyncio-Version (`StreamRecorder` als Coroutine-Klasse); `app.py` orchestriert N Tasks; SIGINT-Via-Signal-Handler | End-to-End & Emit-Equivalent |
| **5** | `cli.py` + `__main__.py` + BWC-Shim `radio_ripper.py` aufrufend | `./run.sh` & `uv run radio-ripper` laufen unverändert |
| **6** | Comprehensive pytest-Suite; Coverage-Report; ruff/mypy clean | CI reif |
| **7** | Pre-Commit, GitHub-Actions, Dockerfile | Bereit für Produktion |
| **8** | arc62 + PlantUML-Diagramme + mdBook-Build | Doku-Vorgabe erfüllt |
| **9** | README aktualisieren, Run.sh hinsichtlich neuer Module justieren | Konsistenz |

---

## 5. Risikoanalyse & Nichteintritt der Funktionsminderung

| Risiko | Absicherung |
|--|--|
| Async-Migration verändert录音rythmik | IcyParser bleibt identische Byte-State-Maschine; Stream-Lese-Loop rein sequenziell mit `async for chunk in response.aiter_bytes()` |
| Reconnect-Backoff ändert Semantik | Decorator `retry_async` verwendet exakt dieselbe Formel `delay *= 2`, max cap wie bisher |
| Komplette-Lieder-Logik bricht | Phase-1-Join-State extrahiert in `IcyRecorderState`-Enum, mit Property-Test besetzt |
| Pydantic vs. bestehendes `config.json` | Settings-Model direkt JSON-ladbar; `load_settings` akzeptiert dateipfad |
| Duplikats-DB-Schema | ALTER TABLE bleibt idempotent; Repositories nutzen bestehendes `songs`-Schema + neue Spalten via wiederverwendbare Migration |
| `run.sh` & `pyproject.toml script` | BWC-Shim leitet `radio_ripper:main` auf `radio_ripper.cli:main` weiter |

---

## 6. Offene Punkte / Rückfragen an Auftrag

1. **Provider-Lizenz**: iTunes Search API ist ok; MusicBrainz würde Offline-Cover-Mirror + zusätzliches Genre liefern — ist das erwünscht als zweiter Adapter?
2. **DB-Migration**: bestehende `ripper.db` existiert bereits in deinem Arbeitsordner — geplantes `ALTER TABLE` ist idempotent, aber solltes Du ein cls-Migration (encoded Schema-Version) bevorzugen? (Default: idempotent ALTER, kein alembic.)
3. **mdBook vs. Antora**: Beide erzeugen HTML-Output; mdBook einfacher (single binary). Wenn du Antora, Hugo oder Sphinx statt mdBook wünschst, bitte rückmelden.
4. **Docker Registry**: Soll das fertige Image nach ghcr.io gepusht werden (GitHub-Action)? Dafür bräuchte ich ggf. einen PAT.
5. **Coverage-Gate**: 85 % strikt via CI blockieren, oder als Information-only?

Antwort auf 1-5 standardmäßig: iTunes-first, idempotent ALTER, mdBook, kein Image-Push, Coverage als Info.

---

Ende des Plans. Nächster Schritt:_Phase 1_ beginnen — Pyproject + Source-Layout aufsetzen.
