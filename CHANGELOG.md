# Changelog – radio-ripper-stream

<!-- version list -->

## v2.13.1 (2026-08-04)

### Bug Fixes

- Prevent PoolTimeout with many concurrent streams
  ([`5551cc0`](https://github.com/domoskanonos/radioripper/commit/5551cc0c761ed93ed15edbaabb4c8386922fcafd))


## v2.13.0 (2026-08-04)

### Features

- Switch to documented JSONC discovery config
  ([`11e6bf6`](https://github.com/domoskanonos/radioripper/commit/11e6bf6e5341b673963f8cd4d7cada017b5fd53e))


## Unreleased

### Fixed

- **PoolTimeout-Fehler bei sehr vielen parallelen Streams behoben:** Der gemeinsame
  HTTP-Connection-Pool war hart auf 400 Verbindungen gedeckelt, während
  `max_concurrent_streams` beliebig groß sein konnte. Dadurch verhungerten Stationen
  jenseits der Pool-Größe beim Verbindungsaufbau und brachen mit `httpx.PoolTimeout`
  ab.
  - Der Pool wird jetzt automatisch aus `max_concurrent_streams` abgeleitet — keine
    zusätzlichen Config-Felder nötig.
  - Idle-Keepalive-Verbindungen werden auf maximal 100 gedeckelt, damit ruhende
    Sockets keine aktiven Streams aus dem Pool verdrängen.
  - Der interne `pool_timeout` wurde von 5 s auf 30 s angehoben, sodass Stationen
    bei kurzzeitiger Pool-Sättigung warten statt sofort zu scheitern.
- Reconnect-Jitter von ±10 % auf ±50 % erhöht, um die Retry-Wellen vieler
  gleichzeitig ausfallender Stationen zu entzerren (Thundering-Herd).
- `ulimit -n` wird in `run.sh` und `docker-entrypoint.sh` auf `8192` (via
  `FD_LIMIT` überschreibbar) angehoben, damit >1000 parallele Streams genügend
  File-Deskriptoren für ihre Sockets haben.

### Changed

- Konfiguration von `config.json` auf kommentierbares `config.jsonc` umgestellt; aktive und Beispielkonfiguration enthalten alle änderbaren Felder mit Erläuterungen.
- Discovery-only-Betrieb dokumentiert und die Zufallsstichprobe standardmäßig auf `50.000` Einträge erhöht.
- `streams`- und `custom.m3u`-Konfigurationspfade sowie die zugehörige Auswahl- und Priorisierungslogik entfernt.
- Die Obergrenze `500` für `max_concurrent_streams` entfernt; nur die Mindestgrenze `1` bleibt bestehen.

## v2.12.0 (2026-08-04)

### Features

- Remove upper limit for max_concurrent_streams and update related tests
  ([`bfa29f8`](https://github.com/domoskanonos/radioripper/commit/bfa29f834838f7117992caa109a9455aca17cd7d))

## v2.11.0 (2026-08-03)

### Features

- Implement move_across_devices function to handle cross-device file moves and update related
  services
  ([`f0e529f`](https://github.com/domoskanonos/radioripper/commit/f0e529ffb4aeeaf0386e63cba8083e7e707673e7))

- Update docker-entrypoint.sh to dynamically build command and remove binary name from CMD in
  Dockerfile
  ([`31dd9df`](https://github.com/domoskanonos/radioripper/commit/31dd9df6dab032d060aff975e17189c56a2ce247))


## v2.10.0 (2026-08-03)

### Features

- Remove optional GitHub PAT from environment and update README for backpressure handling
  ([`0fc53bd`](https://github.com/domoskanonos/radioripper/commit/0fc53bdf6a3623e029cae19f5e76749e0fd8dcb5))


## v2.9.0 (2026-08-03)

### Features

- Update AcoustID configuration and improve queue handling — change destination path, increase max
  inbox files, and refactor queue setup for live settings updates
  ([`4d046f2`](https://github.com/domoskanonos/radioripper/commit/4d046f289c698dcfd057cc263d32c9987100046e))


## v2.8.0 (2026-08-03)

### Features

- Implement backpressure handling for AcoustID queue — add limits for unchecked files and bytes, and
  pause/resume recorders based on storage capacity
  ([`2b92d83`](https://github.com/domoskanonos/radioripper/commit/2b92d83cc171163637b82e36c592412e1fb45111))


## v2.7.0 (2026-08-03)

### Features

- Erweitere AcoustID-Konfiguration — füge neue Parameter für API-Anfragen und Score hinzu
  ([`4205888`](https://github.com/domoskanonos/radioripper/commit/4205888354b426adf888a054d3e2a39bb611fbf6))


## v2.6.0 (2026-08-03)

### Features

- Erweitere URL-Validierung und M3U-Parsing — implementiere Sicherheitsprüfungen und neue
  Hilfsfunktionen
  ([`e6a99b7`](https://github.com/domoskanonos/radioripper/commit/e6a99b74d787b1f1badefe7df54171bb16d7e0b2))


## v2.5.0 (2026-08-03)

### Features

- Erweitere AcoustID-Integration — füge Unterstützung für Metadatenverarbeitung und Staging-Dateien
  hinzu
  ([`e909931`](https://github.com/domoskanonos/radioripper/commit/e909931d5ed2d8532362b9981eced4bcdb1811aa))

- Füge AcoustID-Integration hinzu — implementiere Lookup und Metadatenverarbeitung
  ([`060e616`](https://github.com/domoskanonos/radioripper/commit/060e6168ab55d93259853acf1ab3e398cd91a8d7))


## v2.4.1 (2026-08-03)

### Bug Fixes

- Aktualisiere Mindest-Score für AcoustID auf 0.9 und erhöhe die Version auf 2.3.4
  ([`8192b48`](https://github.com/domoskanonos/radioripper/commit/8192b48d5e26abf73c5153058247553e8f389945))


## v2.4.0 (2026-08-03)

### Features

- AcoustID-Filterung — nur Aufnahmen mit ausreichendem Fingerprint-Score behalten
  ([`2b5fb0f`](https://github.com/domoskanonos/radioripper/commit/2b5fb0fc6384efe7218c9be5a8224d0c5b3d7543))


## [2.3.4] - 2026-08-03

### Added
- **AcoustID-Filterung**: Aufnahmen werden nach dem Fingerprinting via Chromaprint gegen die AcoustID-Datenbank geprüft. Nur Dateien mit einem Match-Score ≥ 0,7 werden behalten – unbekannte oder qualitativ schlechte Aufnahmen werden automatisch verworfen.
- Umgebungsvariable `ACOUST_ID` ist jetzt **Pflichtfeld**: radio-ripper startet nicht ohne einen gültigen AcoustID API-Key (Exit-Code 3).
- `fpcalc` (Chromaprint) wird im Docker-Image automatisch installiert (`libchromaprint-tools`).

### Changed
- Dockerfile: `libchromaprint-tools` zu den Laufzeit-Abhängigkeiten hinzugefügt (neben `ffmpeg`).
- `docker-compose.yml`: `ACOUST_ID`-Variable als Pflichtfeld dokumentiert.
- README aktualisiert: Umgebungsvariablen-Tabelle, Schnellstart-Beispiele, Entwicklungsanleitung und Architektur-Beschreibung.


## v2.3.3 (2026-07-31)

### Bug Fixes

- Verbessere Timeout-Handling in der _probe_batch-Funktion
  ([`736601e`](https://github.com/domoskanonos/radioripper/commit/736601e89f271b059d48744e9f7936a28b32470a))


## v2.3.2 (2026-07-31)

### Bug Fixes

- Modified
  ([`72b97dd`](https://github.com/domoskanonos/radioripper/commit/72b97dd32488a20b0674c029f1458ad8fffea2cf))

### Chores

- Aktualisiere Changelog und füge Versionstoml für Semantic Release hinzu
  ([`5fb7947`](https://github.com/domoskanonos/radioripper/commit/5fb794779158c877042364ed40423edcffd38e74))


## [2.3.0] - 2026-07-31

### Added
- **Config-Reload via Neustart**: Bei Änderungen an relevanten Config-Feldern werden alle Recorder kurz gestoppt und über den gemeinsamen Startpfad neu gestartet (statt Diff-Sync)
- **Cache-Invalidierung per Config-Fingerprint**: Playlist-Discovery-Caches werden automatisch invalidiert, wenn sich relevante Config-Werte ändern (z. B. `max_concurrent_streams`, `stream_keywords`, `discovery_min_bitrate`)
- Konfigurierbares Zielverzeichnis `destination` in der Config
- Automatisches Versions-Tagging im Docker-Workflow (`2.3.0` nur bei Git-Tags, `latest` weiterhin bei jedem main-Push)
- Docker-Image-Label `org.opencontainers.image.version` wird dynamisch aus der pyproject-Version gesetzt

### Changed
- Maximale Anzahl gleichzeitiger Verbindungen auf 400 erhöht
- Playlist-Discovery-Logging und Cache-Verwaltung optimiert

## [2.2.0] - 2026-07-29

### Added
- **Live-Config (Hot-Reload)**: Config-Änderungen werden alle 60s erkannt und live übernommen
- **Inbox-Monitor**: Bei vollem Inbox-Verzeichnis pausieren alle Streams; alle 5 Min wird geprüft, automatische Wiederaufnahme bei ≤80 % des Limits
- **Stream-Pause/Resume-API**: Graceful pause (aktueller Track wird fertig geschrieben) für zentrale Steuerung
- `RadioRipperApp.from_settings_with_live_config()` für integriertes Hot-Reload

### Changed
- Config-Umzug: `config.json` → `config/config.jsonc` (eigenes Unterverzeichnis für einfaches Mounten)
- `config.docker.json` entfernt – der Entrypoint erkennt Config automatisch
- `rec/`-Verzeichnis entfernt – Discovery lädt die M3U selbstständig in `work_dir`
- Tote Config-Felder entfernt: `destination`, `temp_directory`
- Stream-Recorder verwendet `_paused`-Event statt `_inbox_full` für zentral gesteuerte Pausen
- Docker-Entrypoint prüft auf `/app/config/config.jsonc` und setzt `--config` automatisch
- Docker-Image-Benutzer korrigiert (uid 1000 statt 1001)

### Removed
- `config.docker.json` (ersetzt durch Entrypoint-Detection)
- `rec/` (Dev-Relikt – Discovery cacht selbst)
- Config-Felder `destination`, `temp_directory` (wurden vom Code ignoriert)

## [2.1.0] - 2026-07-25

### Added
- Split from monorepo: eigenständiges Projekt mit eigener pyproject.toml, Dockerfile und CI
- Pre-flight reachability check beim Start
- startup_grace_titles (default 2) für sauberen Recording-Start
- Empty StreamTitle='' emittiert jetzt TitleChanged("") für korrekte first_title_seen-Erkennung

### Changed
- Stream-Recording ohne GUI/API-Abhängigkeiten
- Reduzierte Dependencies (nur httpx + pydantic)
