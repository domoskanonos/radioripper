# Changelog – radio-ripper-stream

## [2.1.0] - 2026-07-25

### Added
- Split from monorepo: eigenständiges Projekt mit eigener pyproject.toml, Dockerfile und CI
- Pre-flight reachability check beim Start
- startup_grace_titles (default 2) für sauberen Recording-Start
- Empty StreamTitle='' emittiert jetzt TitleChanged("") für korrekte first_title_seen-Erkennung

### Changed
- Stream-Recording ohne GUI/API-Abhängigkeiten
- Reduzierte Dependencies (nur httpx + pydantic)
