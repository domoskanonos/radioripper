# 1. Einführung und Ziele

## 1.1 Aufgabenbeschreibung

Radio-Ripper ist ein produktionsreifer Webradio-Ripper, der mehrere ICY-Metadaten-Streams **parallel und dauerhaft** im Hintergrund aufzeichnet. Er trennt Lieder automatisch anhand der `StreamTitle`-Wechsel, vermeidet Duplikate über eine lokale SQLite-Datenbank, taggt die MP3-Dateien mit ID3v2 und reichert Metadaten optional über die iTunes Search API an (inklusive Cover-Art).

## 1.2 Qualitätsziele

| # | Qualitätsziel | Motivation |
|---|---|---|
| 1 | **Wartbarkeit** | Klare Schichtung (Hexagonal), kleine Module, Testbarkeit |
| 2 | **Betriebssicherheit** | Automatischer Reconnect mit exponentiellem Backoff, Graceful Shutdown |
| 3 | **Datenintegrität** | Nur komplette Lieder werden gespeichert, Duplikate via SQLite dedup |
| 4 | **Erweiterbarkeit** | Services als ABCs, neue Quellen/Tagger/Provider ohne Core-Änderung |
| 5 | **Typsicherheit** | mypy strict, Pydantic-Validierung der Config |

## 1.3 Stakeholder

| Rolle | Interesse |
|---|---|
| Endbenutzer | Einfacher Start (`run.sh`), keine Duplikate, getaggte MP3s |
| Entwickler | Klares Layout, Tests, mypy clean, CI-Grün |
| Operator | Docker, Health-Check, Graceful Shutdown, PID-Datei |
