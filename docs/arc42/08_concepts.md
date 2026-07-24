# 8. Querschnittskonzepte

## 8.1 Fehlerbehandlung

Exception-Hierarchie in `infra/errors.py`:

```
RadioRipperError
+-- ConfigurationError
+-- StreamConnectionError
+-- StreamProtocolError
+-- RepositoryError
+-- TaggingError
```

Jede Exception ist catch-and-log, niemals silent-fail.

## 8.2 Logging

`logging` mit strukturierter Formatierung, konfiguriert via `infra/logging.py`. Log-Level via Config oder `--log-level` CLI-Override.

## 8.3 Konfiguration

Pydantic-v2-Modelle mit Validierung:
- `Settings` (Top-Level): `destination`, `database`, `work_dir`, `enrich_metadata`, `embed_cover_art`, `acoustid_api_key`, `discovery_enabled`, ...
- `StreamConfig`: `name`, `url`, `output_dir`, ...
- Defaults via `config.json`, uberschreibbar via CLI-Args.
- `work_dir` (Default: `./work`) bundelt Datenbank (`work/ripper.db`), Cache (`work/cache`) und Logs (`work/radio_ripper.log`).

## 8.4 Testing

- 273 Pytests mit pytest-asyncio fur async Code
- `asyncio_mode = "auto"`
- HTTP-Mocking via respx
- Stream-Tests mit Fake `AsyncHttpClient`, kein Real I/O
- Coverage-Gate: 70%

## 8.5 CI/CD

- GitHub Actions bei jedem Push/PR
- lint: Ruff check + format
- type-check: mypy strict
- test: pytest --cov auf Python 3.11-3.13
- GitHub Pages: mkdocs-gerenderte arc42-Dokumentation

## 8.6 Rechtliche Hinweise / Legal Compliance

**DE:** Radio-Ripper ist ein Werkzeug zur privaten, nicht-kommerziellen Aufnahme öffentlich zugänglicher Internet-Radiostreams. Die Software selbst enthält keine DRM-Umgehung, keine Authentifizierung an geschützten Diensten und keine Funktion zum Teilen oder Veröffentlichen von Mitschnitten. Jegliche Nutzung, die gegen geltendes Urheberrecht oder Nutzungsbedingungen Dritter verstösst, liegt in der alleinigen Verantwortung des Nutzers.

**EN:** Radio-Ripper is a tool for private, non-commercial recording of publicly available internet radio streams. The software itself does not circumvent DRM, does not authenticate against protected services, and provides no functionality to share or publish recordings. Any use that violates applicable copyright laws or third-party terms of service is the sole responsibility of the user.
