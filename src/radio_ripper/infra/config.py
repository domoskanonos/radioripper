from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

from radio_ripper import __version__
from radio_ripper.infra.errors import ConfigurationError


def _default_user_agent() -> str:
    return f"Radio-Ripper/{__version__}"


def _strip_jsonc_comments(text: str) -> str:
    """Remove JSONC comments while preserving comment markers inside strings."""
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
                raise ValueError("unterminated JSONC block comment")
            i = end + 2
        else:
            result.append(char)
            i += 1
    return "".join(result)


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
    max_concurrent_streams: int = Field(default=400, ge=1)
    user_agent: str = Field(default_factory=_default_user_agent)
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
    destination: Path = Field(default=Path("./destination"))
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

    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    user_agent: str = Field(default_factory=_default_user_agent)
    min_file_size_bytes: int = Field(default=1572864, ge=0)
    max_files_inbox: int = Field(default=100000, ge=1)
    ignore_title_patterns: list[str] = Field(default_factory=list)
    no_icy_disable_after: int = Field(default=10, ge=1)
    min_file_duration_s: float = Field(default=90, ge=0)

    max_concurrent_streams: int = Field(default=400, ge=1)

    # HTTP connection pool for streaming. The pool must be at least as large as
    # the number of concurrent streams, otherwise requests starve and raise
    # PoolTimeout. 0 means "follow max_concurrent_streams".
    http_pool_size: int = Field(default=0, ge=0)
    # How long a connection waits for a free pool slot before raising PoolTimeout.
    http_pool_timeout: float = Field(default=30.0, ge=1.0)
    # Idle keepalive connections share the same pool slots; keep them well below
    # the pool size so long-lived streams are not starved by idle sockets.
    http_max_keepalive: int = Field(default=100, ge=0)

    # Discovery/Probing settings (previously hardcoded in app.py and playlist_discovery.py)
    probe_timeout: float = Field(default=8.0, ge=1.0)
    probe_concurrent: int = Field(default=20, ge=1, le=100)
    discovery_probe_timeout: float = Field(default=8.0, ge=1.0)
    discovery_max_concurrent: int = Field(default=300, ge=1, le=500)
    discovery_random_sample_size: int = Field(default=50000, ge=100)
    discovery_work_station_count: int = Field(default=400, ge=1)

    # AcoustID settings
    acoustid_min_score: float = Field(default=0.9, ge=0.0, le=1.0)
    acoustid_api_url: str = Field(default="https://api.acoustid.org/v2/lookup")
    # Rate limit: AcoustID allows max 3 req/s (= 180/min). 170/min keeps ~94 %
    # of the ceiling with a small safety margin against network jitter.
    acoustid_requests_per_minute: int = Field(default=170, ge=1, le=180)
    # Max retries for transient API errors (network, timeout) before giving up
    acoustid_retry_max_attempts: int = Field(default=5, ge=0)
    # Base delay for retry backoff (seconds)
    acoustid_retry_base_delay: float = Field(default=30.0, ge=1.0)
    # Max retry delay cap (seconds)
    acoustid_retry_max_delay: float = Field(default=3600.0, ge=60.0)

    # work_dir/unchecked_mp3 IS the AcoustID queue. These are its limits; when
    # exceeded the app pauses all recorders until the queue drains.
    max_unchecked_files: int = Field(default=5000, ge=100)
    max_unchecked_bytes: int = Field(default=10 * 1024 * 1024 * 1024, ge=0)  # 10 GB

    # Logging settings (previously hardcoded in logging.py)
    log_file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)  # 5 MB
    log_file_backup_count: int = Field(default=5, ge=0)

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
            text = cfg_path.read_text(encoding="utf-8")
            raw = json.loads(_strip_jsonc_comments(text))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
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
        updates: dict[str, Any] = {}
        for field in Settings.model_fields:
            old_val = getattr(self._current, field)
            new_val = getattr(new, field)
            if old_val != new_val:
                diff[field] = (old_val, new_val)
                updates[field] = new_val

        # Use model_copy to safely update the frozen model instead of setattr
        if updates:
            self._current = self._current.model_copy(update=updates)

        return diff


__all__ = [
    "DiscoverySettings",
    "LiveConfig",
    "Settings",
    "StreamConfig",
    "StreamSettings",
    "load_settings",
]
