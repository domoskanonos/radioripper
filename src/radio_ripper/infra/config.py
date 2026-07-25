"""Pydantic settings models for radio_ripper configuration.

Loading is performed via :func:`load_settings` which reads a JSON file and
returns a validated :class:`Settings` instance. Invalid configurations raise
:class:`~radio_ripper.infra.errors.ConfigurationError`.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator, model_validator

from radio_ripper.infra.errors import ConfigurationError


class StreamConfig(BaseModel):
    """A single radio station entry (used internally after discovery)."""

    name: str = Field(min_length=1, max_length=64)
    url: HttpUrl
    enabled: bool = True
    ad_title_patterns: list[str] | None = None
    bitrate: int = 0
    icy: bool = True
    source: str = ""

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("stream name must not be empty")
        return v


class Settings(BaseModel):
    """Validated radio_ripper configuration."""

    model_config = {"populate_by_name": True}

    destination: Path = Field(default=Path("./recordings"))

    # Runtime data directory — logs, temp files, inbox all live here.
    work_dir: Path = Field(default=Path("./work"))
    database: Path | None = None
    stream_keywords: list[str] = Field(
        default_factory=lambda: [
            "rock", "50", "60", "70", "80", "90", "10",
            "dance", "pop", "top hits", "charts",
        ]
    )
    discovery_enabled: bool = True
    temp_dir: Path | None = Field(default=None, alias="temp_directory")
    discovery_min_stations: int = Field(default=150, ge=1)
    discovery_min_bitrate: int = Field(default=0, ge=0)

    # Internal — set after discovery, not in config.json
    streams: list[StreamConfig] = Field(default_factory=list, exclude=True)

    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    user_agent: str = "Radio-Ripper/2.0"
    min_file_size_bytes: int = Field(default=1024, ge=0)
    log_level: str = "INFO"
    log_file: Path | None = None

    overwrite_existing_files: bool = False
    ad_title_patterns: list[str] = Field(default_factory=list)
    no_icy_disable_after: int = Field(default=10, ge=1)
    startup_grace_titles: int = Field(default=2, ge=0)

    fallback_cover_path: Path | None = None
    metadata_timeout: float = Field(default=8.0, ge=0.5)
    cover_timeout: float = Field(default=15.0, ge=0.5)

    mp3_inbox: Path | None = Field(default=None, alias="mp3_inbox")
    min_duration_s: float = Field(default=45, ge=0)
    acoustid_min_score: float = Field(default=0.85, ge=0.0, le=1.0)
    min_popularity_rank: int = Field(default=100000, ge=0)
    github_pat: str = ""
    enable_coverartarchive: bool = True

    max_concurrent_streams: int = Field(default=400, ge=1, le=500)
    disable_automatic_streams: bool = False

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator(
        "work_dir",
        "database",
        "destination",
        "log_file",
        "fallback_cover_path",
        "temp_dir",
        "mp3_inbox",
    )
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None

    @model_validator(mode="after")
    def _resolve_work_paths(self) -> Settings:
        if self.database is None:
            self.database = self.work_dir / "ripper.db"
        if self.log_file is None:
            self.log_file = self.work_dir / "radio_ripper.log"
        if self.temp_dir is None:
            self.temp_dir = self.work_dir / "temp"
        if self.mp3_inbox is None:
            self.mp3_inbox = self.work_dir / "mp3_inbox"
        return self


def load_settings(path: str | Path) -> Settings:
    """Load and validate a JSON configuration file.

    Args:
        path: Path to the ``config.json`` file (``~`` is expanded).

    Returns:
        A validated :class:`Settings` instance.

    Raises:
        ConfigurationError: If the file cannot be read or fails validation.
    """
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigurationError(f"config file not found: {cfg_path}")
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read config {cfg_path}: {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid config: {exc}") from exc


__all__ = ["Settings", "StreamConfig", "load_settings"]
