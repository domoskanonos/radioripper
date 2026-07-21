# 10. Qualitatsanforderungen

| Qualitatsmerkmal | Anforderung | Verifikation |
|---|---|---|
| Wartbarkeit | Module < 250 LOC, max 1 Verantwortung | Code-Review |
| Testbarkeit | Tests, Coverage >= 80% | `pytest --cov` |
| Typsicherheit | mypy strict, 0 errors | `uv run mypy` |
| Lint | ruff clean, 0 errors | `uv run ruff check` |
| Datenintegritat | Keine partiellen Songs, keine Dupes | Integrationstest |
| Betriebssicherheit | Graceful Shutdown < 30s | manuell, Signal-Test |
