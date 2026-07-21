# 4. Lösungsstrategie

## 4.1 Architekturstil: Hexagonal / Layered

```
┌───────────────────────────────────────────────┐
│                  CLI / App                     │  <- Entry-Point
├───────────────────────────────────────────────┤
│              Services-Layer                    │
│  StreamRecorder . IcyParser . TrackWriter      │  <- Business-Logic
│  TrackTagger . TrackRepository . Metadata      │
├───────────────────────────────────────────────┤
│               Infra-Layer                      │
│  AsyncHttpClient . Config . Logging . Errors   │  <- Technical
├───────────────────────────────────────────────┤
│               Domain-Layer                     │
│  TrackInfo . SavedTrack . EnrichedInfo         │  <- Pure Models
└───────────────────────────────────────────────┘
```

## 4.2 Schlüsselentscheidungen

| # | Entscheidung | Begründung | ADR |
|---|---|---|---|
| 1 | Async/await statt threading | I/O-bound, leichtere Fehlerbehandlung | [ADR-0001](adrs/0001-async-await-vs-threading.md) |
| 2 | Service-ABCs (Dependenz Injection) | Testbarkeit, Ersetzbarkeit | [ADR-0002](adrs/0002-service-abcs-di.md) |
| 3 | Pydantic v2 für Config | Validierung, Defaults, Schema | [ADR-0003](adrs/0003-pydantic-v2-config.md) |
| 4 | Nur komplette Songs speichern | Datenqualitat > Quantitat | [ADR-0004](adrs/0004-complete-songs-only.md) |
