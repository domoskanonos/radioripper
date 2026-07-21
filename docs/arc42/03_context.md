# 3. Kontext und Überblick

## 3.1 System-Kontext

```
                          ┌──────────────┐
                          │   Internet    │
                          │   Webradio    │
                          │   (.m3u/.pls) │
                          └──────┬───────┘
                                 │ HTTP/ICY
                                 ▼
┌─────────────────────────────────────────────┐
│              Radio-Ripper v2                  │
│                                               │
│  ┌─────────┐  ┌───────────┐  ┌────────────┐  │
│  │ Playlist │  │  Stream   │  │ Metadata   │  │
│  │ Resolver │──│  Recorder │──│ Provider   │  │
│  │         │  │ (per Stream)│ │ (iTunes)   │  │
│  └─────────┘  └─────┬─────┘  └────────────┘  │
│                      │                        │
│              ┌───────┼───────┐                │
│              ▼       ▼       ▼                │
│         TrackWriter │ TrackTagger             │
│         TrackRepo   │                         │
│              │       │                        │
│              ▼       ▼                        │
│         ┌─────────────────┐                   │
│         │  Dateisystem     │                   │
│         │  MP3 + songs.db  │                   │
│         └─────────────────┘                   │
└─────────────────────────────────────────────┘
```

## 3.2 Externe Schnittstellen

| Schnittstelle | Protokoll | Zweck |
|---|---|---|
| Webradio-Stream | HTTP/ICY | Audiostream + Metadaten (icy-metaint) |
| Playlist (.m3u/.pls) | HTTP | Stream-URL-Auflösung |
| iTunes Search API | HTTPS | Metadaten anreichern (Artist, Album, Cover) |
| Dateisystem | POSIX | MP3 schreiben, songs.db, Cover-Bilder |
| System-Signals | SIGINT, SIGTERM | Graceful Shutdown |
