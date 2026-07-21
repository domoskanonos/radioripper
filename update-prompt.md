<KONTEXT>
Ein Open-Source-Projekt muss auf Produktionsreife gebracht werden: vollständige Dokumentation, Qualitätssicherung, CI/CD-Pipeline und automatisierte Deployments.
</KONTEXT>

<AUFGABE>
Alle Lücken in den folgenden Akzeptanzkriterien schließen. Prüfe jedes Kriterium gegen <eingabe_projekt>, dokumentiere den Status, führe die notwendigen Änderungen durch und verifiziere das Ergebnis.
</AUFGABE>

<AKZEPTANZKRITERIEN>

<DOKUMENTATION>
- README.md: Projektbeschreibung, Funktionsumfang, Start/Ausführung, CLI-Referenz (exakt wie `--help`), Konfigurationsoptionen
- docs/arc42/: alle 12 arc42-Abschnitte
- CHANGELOG.md (Semantic Versioning, aktuell)
- CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT.md, .editorconfig
- .github/ISSUE_TEMPLATE/, PULL_REQUEST_TEMPLATE.md
</DOKUMENTATION>

<QUALITAET>
- Testabdeckung ≥ 80 % (projektspezifisches Tool)
- Lockfile aktuell (uv.lock / package-lock.json / go.sum / Cargo.lock / etc.)
- Keine veralteten Major-Versionen bei Abhängigkeiten
- Keine bekannten CVEs (Tool: pip-audit / npm audit / trivy / cargo audit)
- Pre-commit-Hooks: alle `rev:` auf neustem Stand, fehlerfrei über das gesamte Projekt
- Git-Verlauf frei von Secrets (API-Keys, Tokens, Passwörter)
- Keine Build-Artefakte oder generierten Dateien im Repository
</QUALITAET>

<CI_CD>
- CI-Workflow: lint, typecheck, test bei jedem Push/PR – läuft auf GitHub fehlerfrei
- CI-Caching für Abhängigkeiten (Job-Laufzeiten optimieren)
- Sprachversions-Matrix auf aktuell supported Releases (z. B. Python 3.11–3.13, Node 20–22, Go 1.22–1.23)
- GitHub Pages Deploy-Job: mkdocs-gerenderte arc42-Dokumentation
- Pages-URL nach Deploy erreichbar
- Docker-Image (falls vorhanden): Basis-Image aktuell, Build getestet
</CI_CD>

</AKZEPTANZKRITERIEN>

<VORGEHEN>
1. Vollständige Bestandsaufnahme: README, Tests, CI, Docs, Pages, Dependencies, Pre-commit, Docker, Secrets – Abweichungen zu <eingabe_projekt> notieren
2. Lücken schließen: Doku aktualisieren, Tests ergänzen, Dependencies updaten, Hooks erneuern
3. Lokal testen: Lockfile aktualisieren, `pre-commit run --all-files`, Tests + Coverage, ggf. Docker-Build
4. Commit + Push auf GitHub
5. GitHub Actions über MCP triggern, Status prüfen, Fehler fixen, wiederholen bis grün
6. GitHub Pages-URL aufrufen und Verfügbarkeit bestätigen
7. GitHub Release (optional) mit aktuellem Changelog vorbereiten
</VORGEHEN>

<AUSGABEFORMAT>
Pro Kriterium eine Zeile: [ERFUELLT_FEHLT_FIXED] Kriterium. Am Ende eine Zusammenfassung: X/Y Kriterien erfüllt, Z offene Punkte.
</AUSGABEFORMAT>

<eingabe_projekt>
Projektpfad: Der Pfad wo du dich gerade befindest
Sprache/Stack: Python
Verwendete Tools (Test, Coverage, Lint, Audit, Build):
Docker-Image vorhanden (ja/nein):
GitHub-Repo-URL: git@github.com:domoskanonos/radioripper.git
Pages-Branch: main
</eingabe_projekt>

KEINE ERKLÄRUNGEN. KEINE HÖFLICHKEITSFLOSKELN. NUR STATUS UND ÄNDERUNGEN. AUSGABE NUR IM VORGEGEBENEN FORMAT.