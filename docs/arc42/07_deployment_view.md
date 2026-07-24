# 7. Verteilungssicht

## 7.1 Deployment-Optionen

| Modus | Beschreibung |
|---|---|
| **Lokal** | `./run.sh` startet `uv run radio-ripper` als Vordergrund-Prozess |
| **Docker** | `docker compose up` mit gemountetem `./config:/app/config:ro` und `./work:/app/work` |

Siehe: `../diagrams/deployment.puml`
