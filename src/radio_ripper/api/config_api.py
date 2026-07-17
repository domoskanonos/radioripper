"""Config API — load, save, and edit :class:`Settings`.

This module is the single source of truth for the GUI's config state.
It wraps :func:`radio_ripper.infra.config.load_settings` and adds
write-back capability (JSON serialization).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from radio_ripper.infra.config import Settings, StreamConfig, load_settings
from radio_ripper.infra.errors import ConfigurationError

_LOGGER = logging.getLogger(__name__)

__all__ = ["ConfigApi"]


class ConfigApi:
    """Synchronous façade for loading and saving the ripper configuration."""

    def __init__(self, config_path: str | Path) -> None:
        self._path = Path(config_path).expanduser()
        self._settings: Settings | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Settings:
        """Load and validate the config file, cache and return it."""
        self._settings = load_settings(self._path)
        return self._settings

    def settings(self) -> Settings:
        """Return the cached settings (loads if not yet loaded)."""
        if self._settings is None:
            return self.load()
        return self._settings

    def save(self, settings: Settings) -> None:
        """Write *settings* back to the JSON file and update the cache."""
        data = settings.model_dump(mode="json")
        self._path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self._settings = settings
        _LOGGER.info("Config saved to %s", self._path)

    def update_field(self, key: str, value: Any) -> Settings:
        """Return a new :class:`Settings` with *key* set to *value*.

        Does **not** write to disk — call :meth:`save` afterwards.
        Raises :class:`KeyError` if *key* is not a known setting.
        """
        current = self.settings()
        if key not in type(current).model_fields:
            raise KeyError(f"unknown setting: {key!r}")
        new_settings = current.model_copy(update={key: value})
        return new_settings

    def reload(self) -> Settings:
        """Force-reload from disk."""
        return self.load()

    @staticmethod
    def make_stream(name: str, url: str) -> StreamConfig:
        """Create and validate a single :class:`StreamConfig`."""
        return StreamConfig(name=name, url=url)

    @staticmethod
    def default_settings() -> Settings:
        """Return a :class:`Settings` with one placeholder stream (for new configs)."""
        return Settings(
            destination=Path("./recordings"),
            database=Path("./recordings/ripper.db"),
            streams=[StreamConfig(name="TopHits", url="http://tophits.radiomonster.fm/listen.m3u")],
        )