# 01 — Einleitung & Ziele

## Zweck

Radio-Ripper zeichnet Webradio-Streams mit ICY-Metadaten dauerhaft und parallel
auf. Er erkennt Songtitel in Echtzeit, trennt Aufnahmen sauber an Songgrenzen,
validiert jede Aufnahme (Größe + Dauer) und identifiziert sie anschließend über
**AcoustID**. Erfolgreich identifizierte Songs werden mit ID3-Tags angereichert
(Artist, Title, Album, Jahr, MusicBrainz-IDs) und in eine standardmäßige
Ordnerstruktur verschoben: `Artist/Album/Artist - Title.mp3`.

## Qualitätsziele

| Ziel | Priorität | Erklärung |
|------|-----------|-----------|
| **Robustheit** | hoch | Auto-Reconnect mit Backoff, Disable toter Sender, Crash-sichere `.part`-Dateien |
| **Einfachheit** | hoch | Ein CLI-Tool, keine API, keine Datenbank; Konfiguration über eine JSONC-Datei |
| **Zuverlässige Identifikation** | mittel | AcoustID-Fingerprint mit Score-Schwelle; kein Treffer ⇒ Datei wird verworfen |
| **Testbarkeit** | hoch | 90 %+ Testabdeckung, klare Modul-Grenzen |

## Stakeholder

| Stakeholder | Interesse |
|-------------|-----------|
| Endnutzer | Automatische MP3-Sammlung ohne manuelle Arbeit |
| Betreiber (Docker) | Stabiler Langzeitbetrieb, Graceful Shutdown |
