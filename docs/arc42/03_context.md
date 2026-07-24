# 3. Kontext und Überblick

## 3.1 System-Kontext

```
                          ┌──────────────┐
                          │   Internet    │
                          │  Webradio +   │
                          │ Playlists     │
                          │ (.m3u/.pls)   │
                          └──────┬───────┘
                                 │ HTTP/ICY
                                 ▼
┌─────────────────────────────────────────────┐
│              Radio-Ripper v2                  │
│                                               │
│  ┌─────────┐  ┌───────────┐  ┌────────────┐  │
│  │ Playlist │  │  Stream   │  │ Metadata   │  │
│  │ Resolver │──│  Recorder │──│ Provider   │  │
│  │  (+ Disc)│  │ (per Stream)│ │(iTunes+CAA)│  │
│  └─────────┘  └─────┬─────┘  └────────────┘  │
│                     │         ┌────────────┐  │
│                     │         │Fingerprint │  │
│                     │         │(AcoustID)  │  │
│                     │         └────────────┘  │
│             ┌───────┼───────┐                 │
│             ▼       ▼       ▼                 │
│        TrackWriter │ TrackTagger              │
│        TrackRepo   │                          │
│             │       │                         │
│             ▼       ▼                         │
│        ┌─────────────────┐                    │
│        │  Dateisystem     │                    │
│        │  MP3 + ripper.db │                    │
│        └─────────────────┘                    │
└─────────────────────────────────────────────┘
```

## 3.2 Externe Schnittstellen

| Schnittstelle | Protokoll | Zweck |
|---|---|---|
| Webradio-Stream | HTTP/ICY | Audiostream + Metadaten (icy-metaint) |
| Playlist (.m3u/.pls) | HTTP | Stream-URL-Auflösung |
| iTunes Search API | HTTPS | Metadaten anreichern (Artist, Album, Cover) |
| AcoustID API | HTTPS | Audio-Fingerprinting + MusicBrainz-Abfrage |
| Cover Art Archive (CAA) | HTTPS | Fallback-Cover-Quelle (MusicBrainz) |
| Dateisystem | POSIX | MP3 schreiben, `ripper.db`, Cover-Bilder |
| System-Signals | SIGINT, SIGTERM | Graceful Shutdown |
