# arc42 — Systemdokumentation Radio-Ripper

Diese Dokumentation folgt dem [arc42-Standard](https://arc42.org) und beschreibt
die Architektur des Webradio-Stream-Recorders **Radio-Ripper**.

## Inhaltsverzeichnis

| Kapitel | Titel | Inhalt |
|---------|-------|--------|
| [01](01_einleitung.md) | Einleitung & Ziele | Zweck, Ziele, Qualitätsziele |
| [03](03_kontext.md) | Kontext | Fachlicher & technischer Kontext |
| [05](05_architektur.md) | Bausteinansicht | Module, Schichten, Abhängigkeiten |
| [06](06_laufzeit.md) | Laufzeitsicht | Kernabläufe (Record → Validate → AcoustID → Tag → Move) |
| [07](07_verteilung.md) | Verteilung | Docker-Deployment, Pfade, Umgebung |

## Diagramme (PlantUML)

Die Diagramme liegen als `.puml`-Dateien unter [`diagrams/`](diagrams/):

| Datei | Zweck |
|-------|-------|
| `kontext.puml` | System- & Fachkontext |
| `bausteine.puml` | Komponentendiagramm der Module |
| `laufzeit_recorder.puml` | Sequence-Diagramm: Aufnahme → AcoustID → Tag → Move |
| `deployment.puml` | Docker-Deployment |

> **Rendern:** `plantuml diagrams/*.puml` (CLI) oder https://www.plantuml.com/plantuml.
