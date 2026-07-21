# ADR-0003: Pydantic v2 fur Config-Validierung

**Status:** Angenommen

**Kontext:** Die Konfiguration wird als JSON-Datei bereitgestellt und muss valide Werte enthalten, bevor der Ripper startet.

**Entscheidung:** Pydantic v2 mit `BaseModel` und `Field`-Validierung. Fehlerhafte Config fuhrt zu sofortigem, klar formulierten Fehler.

**Konsequenzen:** Typkonvertierung und Validierung sind deklarativ. Schema kann per `model_json_schema()` exportiert werden.
