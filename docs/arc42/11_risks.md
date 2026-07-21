# 11. Risiken und technische Schulden

| # | Risiko | Mitigation |
|---|---|---|
| 1 | iTunes API kann Rate-Limit | Backoff in MetadataProvider |
| 2 | Stream sendet keine ICY-Metadaten | Detektion, Logs, Stream wird ubersprungen |
| 3 | SQLite als Single-Writer | asyncio.Lock + to_thread |
| 4 | Mutagen hat keine Type-Stubs | `# mypy: disable-error-code` in tagging.py |
