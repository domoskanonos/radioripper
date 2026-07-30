from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

from radio_ripper.infra.errors import ConfigurationError


class StreamConfig(BaseModel):
    model_config = {"frozen": True}

    name: str = Field(min_length=1, max_length=64)
    url: HttpUrl
    enabled: bool = True
    ignore_title_patterns: list[str] | None = None
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


class StreamSettings(BaseModel):
    max_concurrent_streams: int = Field(default=400, ge=1, le=500)
    user_agent: str = "Radio-Ripper/2.0"
    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    no_icy_disable_after: int = Field(default=10, ge=1)
    ignore_title_patterns: list[str] = Field(default_factory=list)
    min_file_size_bytes: int = Field(default=1572864, ge=0)
    max_files_inbox: int = Field(default=100000, ge=1)
    min_file_duration_s: float = Field(default=90, ge=0)


class DiscoverySettings(BaseModel):
    discovery_enabled: bool = True
    stream_keywords: list[str] = Field(
        default_factory=lambda: [
            "rock",
            "50",
            "60",
            "70",
            "80",
            "90",
            "10",
            "dance",
            "pop",
            "top hits",
            "charts",
        ]
    )
    discovery_min_bitrate: int = Field(default=0, ge=0)


class Settings(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    work_dir: Path = Field(default=Path("./work"))
    destination: Path = Field(default=Path("/home/laptop/trash/mp3"))
    log_level: str = "INFO"

    stream_keywords: list[str] = Field(
        default_factory=lambda: [
            "rock",
            "50",
            "60",
            "70",
            "80",
            "90",
            "10",
            "dance",
            "pop",
            "top hits",
            "charts",
        ]
    )
    discovery_enabled: bool = True
    discovery_min_bitrate: int = Field(default=0, ge=0)

    streams: list[StreamConfig] = Field(default_factory=list, exclude=True)

    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    user_agent: str = "Radio-Ripper/2.0"
    min_file_size_bytes: int = Field(default=1572864, ge=0)
    max_files_inbox: int = Field(default=100000, ge=1)
    ignore_title_patterns: list[str] = Field(default_factory=list)
    no_icy_disable_after: int = Field(default=10, ge=1)
    min_file_duration_s: float = Field(default=90, ge=0)

    max_concurrent_streams: int = Field(default=400, ge=1, le=500)

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("work_dir", "destination")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    @property
    def stream(self) -> StreamSettings:
        return StreamSettings(
            max_concurrent_streams=self.max_concurrent_streams,
            user_agent=self.user_agent,
            request_timeout=self.request_timeout,
            reconnect_base_delay=self.reconnect_base_delay,
            reconnect_max_delay=self.reconnect_max_delay,
            no_icy_disable_after=self.no_icy_disable_after,
            ignore_title_patterns=self.ignore_title_patterns,
            min_file_size_bytes=self.min_file_size_bytes,
            max_files_inbox=self.max_files_inbox,
            min_file_duration_s=self.min_file_duration_s,
        )

    @property
    def discovery(self) -> DiscoverySettings:
        return DiscoverySettings(
            discovery_enabled=self.discovery_enabled,
            stream_keywords=self.stream_keywords,
            discovery_min_bitrate=self.discovery_min_bitrate,
        )


def load_settings(path: str | Path | None = None) -> Settings:
    if path is not None:
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
    return Settings()


class LiveConfig:
    """Watches a config file for mtime changes and hot-reloads Settings.

    Mutates the *same* Settings object in-place so that all existing references
    (e.g. in StreamRecorder) see the new values.
    """

    def __init__(self, path: str | Path, initial: Settings) -> None:
        self._path = Path(path).expanduser()
        self._mtime = self._path.stat().st_mtime
        self._current = initial

    @property
    def settings(self) -> Settings:
        return self._current

    @property
    def path(self) -> Path:
        return self._path

    async def check_reload(self) -> dict[str, tuple[Any, Any]]:
        """Check mtime; if changed, reload & mutate settings in-place.

        Returns a dict of {field_name: (old_value, new_value)} for changed
        fields, or an empty dict when nothing changed.
        """
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return {}
        if mtime <= self._mtime:
            return {}
        try:
            new = load_settings(self._path)
        except ConfigurationError:
            return {}

        self._mtime = mtime
        diff: dict[str, tuple[Any, Any]] = {}
        for field in Settings.model_fields:
            old_val = getattr(self._current, field)
            new_val = getattr(new, field)
            if old_val != new_val:
                diff[field] = (old_val, new_val)
                setattr(self._current, field, new_val)
        return diff


__all__ = [
    "DiscoverySettings",
    "LiveConfig",
    "Settings",
    "StreamConfig",
    "StreamSettings",
    "load_settings",
]
