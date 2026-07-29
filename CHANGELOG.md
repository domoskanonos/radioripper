# Changelog – radio-ripper-stream

## [2.2.0] - 2026-07-29

### Added
- **Live-Config (Hot-Reload)**: Config-Änderungen werden alle 60s erkannt und live übernommen
- **Inbox-Monitor**: Bei vollem Inbox-Verzeichnis pausieren alle Streams; alle 5 Min wird geprüft, automatische Wiederaufnahme bei ≤80 % des Limits
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
