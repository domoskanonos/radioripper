# Changelog – radio-ripper-stream

<!-- version list -->

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
- Config-Umzug: `config.json` → `config/config.json` (eigenes Unterverzeichnis für einfaches Mounten)
- `config.docker.json` entfernt – der Entrypoint erkennt Config automatisch
- `rec/`-Verzeichnis entfernt – Discovery lädt die M3U selbstständig in `work_dir`
- Tote Config-Felder entfernt: `destination`, `temp_directory`
- Stream-Recorder verwendet `_paused`-Event statt `_inbox_full` für zentral gesteuerte Pausen
- Docker-Entrypoint prüft auf `/app/config/config.json` und setzt `--config` automatisch
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
