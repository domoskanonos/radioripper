# 6. Laufzeitsicht

## 6.1 Happy Path: Song-Aufzeichnung

Siehe: `../diagrams/sequence-recording.puml`

1. `StreamRecorder._run_forever()` -> `_run_once()`
2. HTTP-Verbindung zum Stream-URL (via `HttpxAsyncClient`)
3. `IcyParser` konsumiert Bytes -> emittiert `AudioChunk` + `TitleChanged`
4. Bei `TitleChanged`: alter Song wird abgeschlossen (TrackWriter.commit -> atomic rename)
5. `TrackRepository.exists()` pruft Duplikat
6. Falls neu: `TrackTagger.tag()` schreibt ID3v2
7. `TrackRepository.register()` tragt in SQLite ein
8. Optional async: `MetadataProvider.enrich()` (nicht-blockierend)

## 6.2 Reconnect mit Backoff

```
  Fehler -> delay = initial_reconnect_delay
     +--> sleep(delay, cancellable via stop_event)
         +--> delay = min(delay * 2, reconnect_max_delay) -> retry
```

## 6.3 Graceful Shutdown

```
  SIGINT/SIGTERM -> stop_event.set()
     +--> _run_forever loop break
         +--> in-flight TrackWriter.discard()  (partial song = wegwerfen)
             +--> PlaylistResolver.aclose(), TrackRepository.aclose()
```
