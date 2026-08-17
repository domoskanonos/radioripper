# 03 — Kontext

## Fachlicher Kontext

```
┌─────────────┐   ICY-Metadaten    ┌──────────────────┐
│ Radiostreams │ ─────────────────▶ │   Radio-Ripper   │
│ (custom.m3u) │                    │                  │
└─────────────┘                    │  nimmt auf,      │
                                   │  validiert,      │
┌──────────────┐   Fingerprint     │  identifiziert   │
│ AcoustID API │ ◀──────────────── │  taggt,          │
└──────────────┘                   │  verschiebt      │
        ▲                          └────────┬─────────┘
        │ Audio-Fingerprint                 │ getaggte MP3
┌───────┴────────┐                 ┌────────▼─────────┐
│ fpcalc (lokal) │                 │ destination/     │
└────────────────┘                 │ Artist/Album/    │
                                   └──────────────────┘
```

## Technischer Kontext

| System | Rolle | Protokoll |
|--------|-------|-----------|
| **Radio-Streams** | Audio-Quellen (MP3, ICY-Metadaten) | HTTP(S), `Icy-MetaData: 1` |
| **AcoustID API** | Audio-Identifikation (Fingerprint → Metadata) | HTTPS/JSON |
| **fpcalc** | Lokaler Chromaprint-Fingerprint | Subprozess |
| **ffprobe** | Dauer-Bestimmung | Subprozess |
| **mutagen** | ID3-Tag-Schreiben | Python-Bibliothek |

## Externe Pfade

| Pfad | Bedeutung |
|------|-----------|
| `work_dir/stations/custom.m3u` | Senderliste (M3U) |
| `work_dir/recordings/` | Staging für validierte Aufnahmen |
| `destination/` | Endziel (getaggte MP3s) |
| `config/config.jsonc` | Konfiguration (JSONC mit Kommentaren) |
