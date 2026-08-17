# 07 — Verteilung

## Docker-Deployment

```
┌──────────────────────────────────────────┐
│ Docker-Container (radio-ripper-stream)   │
│                                          │
│  ┌──────────────┐                        │
│  │  radio-ripper │  CLI (workflow.main)  │
│  │  (python)     │                       │
│  └──────┬───────┘                        │
│         │                                │
│  ┌──────▼───────┐  ┌──────────────────┐  │
│  │  fpcalc      │  │  ffprobe         │  │
│  └──────┬───────┘  └──────────────────┘  │
└─────────┼────────────────────────────────┘
          │
  ┌───────▼───────┐
  │ /app/config   │  config.jsonc (ro)
  ├───────────────┤
  │ /app/work     │  recordings/, stations/
  ├───────────────┤
  │ /app/destination │  Artist/Album/*.mp3
  └───────────────┘
```

## Umgebungsvariablen

| Variable | Zweck |
|----------|-------|
| `ACOUST_ID` | AcoustID-API-Key (Pflicht für Identifikation) |

## Pfade (im Container)

| Pfad | Zweck | Mount |
|------|-------|-------|
| `/app/config/config.jsonc` | Konfiguration | `:ro` |
| `/app/work` | Staging (recordings/, stations/) | Volume |
| `/app/destination` | Fertige MP3s | Volume |

## Start

```bash
docker run --rm \
  -e ACOUST_ID=<key> \
  -v "$PWD/config:/app/config:ro" \
  -v "$PWD/work:/app/work" \
  -v "$PWD/destination:/app/destination" \
  domoskanonos/radio-ripper-stream
```

## Externe Systeme zur Laufzeit

- Radio-Streams (Internet, HTTP)
- AcoustID API (`api.acoustid.org`)
