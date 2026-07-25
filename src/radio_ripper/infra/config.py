from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl, SecretStr, ValidationError, field_validator, model_validator

from radio_ripper.infra.errors import ConfigurationError


class StreamConfig(BaseModel):
    model_config = {"frozen": True}

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


class StreamSettings(BaseModel):
    max_concurrent_streams: int = Field(default=400, ge=1, le=500)
    user_agent: str = "Radio-Ripper/2.0"
    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    no_icy_disable_after: int = Field(default=10, ge=1)
    startup_grace_titles: int = Field(default=2, ge=0)
    ad_title_patterns: list[str] = Field(default_factory=list)
    min_file_size_bytes: int = Field(default=102400, ge=0)
    overwrite_existing_files: bool = False
    min_duration_s: float = Field(default=45, ge=0)


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
    discovery_min_stations: int = Field(default=150, ge=1)
    discovery_min_bitrate: int = Field(default=0, ge=0)
    disable_automatic_streams: bool = False


class StorageSettings(BaseModel):
    model_config = {"populate_by_name": True}

    destination: Path = Field(default=Path("./recordings"))
    work_dir: Path = Field(default=Path("./work"))
    temp_dir: Path | None = Field(default=None, alias="temp_directory")
    mp3_inbox: Path | None = Field(default=None, alias="mp3_inbox")

    @field_validator("work_dir", "destination", "temp_dir", "mp3_inbox")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None


class LoggingSettings(BaseModel):
    log_level: str = "INFO"
    log_file: Path | None = None

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("log_file")
    @classmethod
    def _expand_log(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None


class Settings(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    destination: Path = Field(default=Path("./recordings"))
    work_dir: Path = Field(default=Path("./work"))
    log_level: str = "INFO"
    log_file: Path | None = None

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
    temp_dir: Path | None = Field(default=None, alias="temp_directory")
    discovery_min_stations: int = Field(default=150, ge=1)
    discovery_min_bitrate: int = Field(default=0, ge=0)

    streams: list[StreamConfig] = Field(default_factory=list, exclude=True)

    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    user_agent: str = "Radio-Ripper/2.0"
    min_file_size_bytes: int = Field(default=102400, ge=0)
    overwrite_existing_files: bool = False
    ad_title_patterns: list[str] = Field(default_factory=list)
    no_icy_disable_after: int = Field(default=10, ge=1)
    startup_grace_titles: int = Field(default=2, ge=0)

    mp3_inbox: Path | None = Field(default=None, alias="mp3_inbox")
    min_duration_s: float = Field(default=45, ge=0)

    max_concurrent_streams: int = Field(default=400, ge=1, le=500)
    disable_automatic_streams: bool = False

    github_pat: SecretStr = Field(default=SecretStr(""))

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("work_dir", "destination", "temp_dir", "mp3_inbox", "log_file")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None

    @model_validator(mode="after")
    def _resolve_work_paths(self) -> Settings:
        if self.log_file is None:
            self.log_file = self.work_dir / "radio_ripper.log"
        if self.temp_dir is None:
            self.temp_dir = self.work_dir / "temp"
        if self.mp3_inbox is None:
            self.mp3_inbox = self.work_dir / "mp3_inbox"
        return self

    @property
    def stream(self) -> StreamSettings:
        return StreamSettings(
            max_concurrent_streams=self.max_concurrent_streams,
            user_agent=self.user_agent,
            request_timeout=self.request_timeout,
            reconnect_base_delay=self.reconnect_base_delay,
            reconnect_max_delay=self.reconnect_max_delay,
            no_icy_disable_after=self.no_icy_disable_after,
            startup_grace_titles=self.startup_grace_titles,
            ad_title_patterns=self.ad_title_patterns,
            min_file_size_bytes=self.min_file_size_bytes,
            overwrite_existing_files=self.overwrite_existing_files,
            min_duration_s=self.min_duration_s,
        )

    @property
    def discovery(self) -> DiscoverySettings:
        return DiscoverySettings(
            discovery_enabled=self.discovery_enabled,
            stream_keywords=self.stream_keywords,
            discovery_min_stations=self.discovery_min_stations,
            discovery_min_bitrate=self.discovery_min_bitrate,
            disable_automatic_streams=self.disable_automatic_streams,
        )

    @property
    def storage(self) -> StorageSettings:
        return StorageSettings(
            destination=self.destination,
            work_dir=self.work_dir,
            temp_directory=self.temp_dir,
            mp3_inbox=self.mp3_inbox,
        )

    @property
    def logging(self) -> LoggingSettings:
        return LoggingSettings(
            log_level=self.log_level,
            log_file=self.log_file,
        )


def load_settings(path: str | Path) -> Settings:
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


__all__ = [
    "DiscoverySettings",
    "LoggingSettings",
    "Settings",
    "StorageSettings",
    "StreamConfig",
    "StreamSettings",
    "load_settings",
]
