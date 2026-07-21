# Dockerfile — Radio-Ripper v2 multi-stage build
#
# Build:
#   docker build -t radio-ripper:2.0 .
# Run:
#   docker run --rm --name ripper \
#     -v "$PWD/config.json:/app/config.json:ro" \
#     -v "$PWD/recordings:/app/recordings" \
#     -v "$PWD/songs.db:/app/songs.db" \
#     radio-ripper:2.0
#
# Strg+C / docker stop → graceful SIGTERM shutdown.

# ── Stage 1: Builder ──────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml LICENSE README.md uv.lock* ./
RUN uv sync --no-install-project --quiet

# Install the project itself
COPY src/ src/
RUN uv sync --quiet

# ── Stage 2: Runtime ─────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="radio-ripper" \
      org.opencontainers.image.version="2.1.0" \
      org.opencontainers.image.description="Webradio-Ripper with ICY parsing, SQLite dedup, ID3v2 tagging & Gradio GUI" \
      org.opencontainers.image.source="https://github.com/domoskanonos/radioripper"

# Non-root user for security
RUN groupadd --system --gid 1001 ripper \
 && useradd --system --uid 1001 --gid ripper --home-dir /app --shell /usr/sbin/nologin ripper

WORKDIR /app

# Copy the virtual environment from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/LICENSE /app/

# Put uv's python on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

# ffmpeg for MP3 frame-alignment post-processing
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Directory for recordings and database
RUN mkdir -p /app/recordings \
 && chown -R ripper:ripper /app

USER ripper

ENTRYPOINT ["radio-ripper"]
CMD ["--config", "/app/config.json"]
