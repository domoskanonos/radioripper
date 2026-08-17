# 06 — Laufzeitsicht

## Kernablauf: Ein Track

```
Recorder                  Validation             AcoustID-Worker
    │                          │                       │
    │ ICY-Titelwechsel         │                       │
    │────────────────────────▶│                       │
    │                          │                       │
    │ TrackWriter (recordings/)│                       │
    │◀─────────────────────────│                       │
    │                          │                       │
    │ Titelwechsel → commit    │                       │
    │─────────────────────────▶│                       │
    │ Größe ≥ 1,5 MB?          │                       │
    │ Dauer ≥ 90 s?            │                       │
    │                          │                       │
    │ enqueue(path)            │                       │
    │────────────────────────────────────────────────▶│
    │                          │                       │ fpcalc → Fingerprint
    │                          │                       │ AcoustID-Lookup
    │                          │                       │ Score ≥ 0,9?
    │                          │                       │ ├─ nein → löschen
    │                          │                       │ └─ ja  → ID3-Tags
    │                          │                       │        → destination/
    │                          │                       │        (Kollision:
    │                          │                       │         besserer Score)
```

## Ablauf im Detail

1. **Start (`workflow.run_stations`)**
   - `.part`-Reste aufräumen
   - Sender aus `custom.m3u` laden
   - ThreadPool (Größe = Sender-Anzahl)
   - AcoustID-Worker starten
   - Pro Sender einen `StreamRecorder` starten

2. **Aufnahme (`recorder._stream_with_meta`)**
   - HTTP-Stream mit `Icy-MetaData: 1` öffnen
   - ICY-Metadaten parsen (AudioChunk / TitleChanged)
   - Erster Titel wird übersprungen („mitten im Song eingestiegen")
   - Ab dem nächsten Titelwechsel wird ein `TrackWriter` gefüllt
   - Beim übernächsten Titelwechsel: `commit()` → Validierung → enqueue

3. **Validierung (`validation.validate_file`)**
   - Größe ≥ `min_file_size_bytes` (sonst löschen)
   - Dauer ≥ `min_file_duration_s` via ffprobe (sonst löschen)

4. **AcoustID (`acoustid.finalize_acoustid`)** — im Singleton-Worker
   - `fpcalc` → Fingerprint + Dauer
   - Lookup → bester Treffer ≥ `min_score`
   - Kein Treffer → Datei löschen
   - Treffer → ID3-Tags schreiben → nach `destination/Artist/Album/` verschieben
   - Kollision → höherer Score gewinnt (geschützt durch Lock)

5. **Shutdown**
   - Recorder stoppen (parallel), Worker beenden (Queue abarbeiten), Executor schließen

## Fehlerpfade

| Fehler | Verhalten |
|--------|-----------|
| Verbindungsabbruch | Reconnect mit exponentiellem Backoff + Jitter |
| Kein ICY nach N Versuchen | Sender deaktivieren |
| N Verbindungsfehler | Sender deaktivieren |
| AcoustID API-Fehler | Datei bleibt in `recordings/` (kein Verlust) |
| Kein AcoustID-Treffer | Datei wird gelöscht |
