# ADR-0001: Async/await statt Threading

**Status:** Angenommen

**Kontext:** Der Ripper muss mehrere Streams parallel verarbeiten. Klassisches Threading in Python leidet unter GIL-Problemen und macht graceful shutdown komplexer.

**Entscheidung:** Wir verwenden `asyncio` mit `async`/`await`. Jeder Stream bekommt einen eigenen Task, kooperatives Scheduling vermeidet Race-Conditions.

**Konsequenzen:** Alle I/O-Operationen mussen async sein. Externe Libraries ohne async-Support werden via `asyncio.to_thread` ausgelagert.
