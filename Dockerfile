# Dockerfile — radio-ripper-stream
# Build:   docker build -t radio-ripper-stream:latest .
# Run:     mkdir -p radio-ripper-config radio-ripper-mp3 radio-ripper-work
#          docker run --rm --name ripper-stream \
#            -v "$PWD/radio-ripper-config:/app/config:ro" \
#            -v "$PWD/radio-ripper-mp3:/app/destination" \
#            -v "$PWD/radio-ripper-work:/app/work" \
#            radio-ripper-stream:latest

FROM python:3.12-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml LICENSE README.md uv.lock* ./
RUN uv sync --no-install-project --quiet

COPY src/ src/
RUN uv sync --quiet

FROM python:3.12-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="radio-ripper-stream" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Webradio stream recorder — ICY metadata, parallel recording, auto-discovery" \
      org.opencontainers.image.source="https://github.com/domoskanonos/radioripper"

RUN groupadd --system --gid 1000 ripper \
 && useradd --system --uid 1000 --gid ripper --home-dir /app --shell /usr/sbin/nologin ripper

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/LICENSE /app/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY docker-entrypoint.sh /usr/local/bin/

RUN mkdir -p /app/recordings /app/work /app/config \
 && chown -R ripper:ripper /app \
 && chmod +x /usr/local/bin/docker-entrypoint.sh

USER ripper

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["radio-ripper"]
