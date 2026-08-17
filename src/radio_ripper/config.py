"""config.py — Konfiguration für radio-ripper."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


def _strip_jsonc_comments(text: str) -> str:
    """Entfernt JSONC-Kommentare, bewahrt Kommentar-Marker in Strings."""
    result: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        char = text[i]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            if newline == -1:
                break
            result.append("\n")
            i = newline + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminierter JSONC-Blockkommentar")
            i = end + 2
        else:
            result.append(char)
            i += 1
    return "".join(result)


class Settings(BaseModel):
    """Minimale Konfiguration für das Streaming-Modul."""

    model_config = {"extra": "ignore"}

    work_dir: Path = Field(default=Path("./work"))
    destination: Path = Field(default=Path("./destination"))
    log_level: str = "INFO"

    # Stream
    max_concurrent_streams: int = Field(default=500, ge=1)
    user_agent: str = "VLC/3.0.18 LibVLC/3.0.18"
    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    no_icy_disable_after: int = Field(default=10, ge=1)
    ignore_title_patterns: list[str] = Field(default_factory=list)

    # Die 2 Validierungs-Tests
    min_file_size_bytes: int = Field(default=1_572_864, ge=0)  # 1.5 MB
    min_file_duration_s: float = Field(default=90.0, ge=0)  # 90 Sekunden

    # AcoustID (Fingerprinting + Tagging)
    acoustid_api_key: str = ""
    acoustid_min_score: float = Field(default=0.9, ge=0.0, le=1.0)

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"ungültiges log_level: {v}")
        return v

    @field_validator("work_dir", "destination")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


def load_settings(path: str | Path | None = None) -> Settings:
    """Lädt Settings aus einer JSONC-Datei oder nutzt Defaults."""
    if path is None:
        return Settings()
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config nicht gefunden: {cfg_path}")
    text = cfg_path.read_text(encoding="utf-8")
    raw = json.loads(_strip_jsonc_comments(text))
    return Settings.model_validate(raw)
