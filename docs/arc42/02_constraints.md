# 2. Randbedingungen

| Aspekt | Entscheidung |
|---|---|
| Programmiersprache | Python >= 3.11 |
| Build-Tool | uv (hatchling backend) |
| Konfiguration | `config.json` (Pydantic v2 validiert) |
| Single-Entry-Point | `uv run radio-ripper --config config.json` |
| Runtime | Lokal via `run.sh` oder Docker-Container |
| Betriebsart | Long-running Prozess, SIGINT/SIGTERM -> Graceful Shutdown |
