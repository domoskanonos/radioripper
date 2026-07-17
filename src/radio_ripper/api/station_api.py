"""Station API — CRUD operations on the ``streams`` list in :class:`Settings`.

All methods return a **new** :class:`Settings` instance (immutable update).
The GUI is responsible for calling :meth:`ConfigApi.save` to persist.
"""

from __future__ import annotations

from radio_ripper.api.config_api import ConfigApi
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.infra.errors import ConfigurationError

__all__ = ["StationApi"]


class StationApi:
    """Manage radio stations (add, edit, remove, list)."""

    def __init__(self, config_api: ConfigApi) -> None:
        self._config_api = config_api

    def list_stations(self) -> list[dict[str, str]]:
        """Return stations as a list of ``{"name": ..., "url": ...}`` dicts."""
        settings = self._config_api.settings()
        return [
            {"name": s.name, "url": str(s.url)}
            for s in settings.streams
        ]

    def add_station(self, name: str, url: str) -> Settings:
        """Create a new :class:`StreamConfig`, append it, return updated settings."""
        name = name.strip()
        if not name:
            raise ConfigurationError("Station name must not be empty.")
        settings = self._config_api.settings()
        existing = {s.name.lower() for s in settings.streams}
        if name.lower() in existing:
            raise ConfigurationError(f"Station '{name}' already exists.")
        new_stream = StreamConfig(name=name, url=url)
        return settings.model_copy(
            update={"streams": [*settings.streams, new_stream]}
        )

    def edit_station(self, old_name: str, new_name: str, new_url: str) -> Settings:
        """Rename / re-URL an existing station. Returns updated settings."""
        new_name = new_name.strip()
        if not new_name:
            raise ConfigurationError("Station name must not be empty.")
        settings = self._config_api.settings()
        other_names = {s.name.lower() for s in settings.streams if s.name != old_name}
        if new_name != old_name and new_name.lower() in other_names:
            raise ConfigurationError(f"Station name '{new_name}' already in use.")
        streams: list[StreamConfig] = []
        found = False
        for s in settings.streams:
            if s.name == old_name:
                streams.append(StreamConfig(name=new_name, url=new_url))
                found = True
            else:
                streams.append(s)
        if not found:
            raise ConfigurationError(f"Station '{old_name}' not found.")
        return settings.model_copy(update={"streams": streams})

    def remove_station(self, name: str) -> Settings:
        """Remove the station named *name*. Returns updated settings."""
        settings = self._config_api.settings()
        streams = [s for s in settings.streams if s.name != name]
        if len(streams) == len(settings.streams):
            raise ConfigurationError(f"Station '{name}' not found.")
        if not streams:
            raise ConfigurationError("Cannot remove the last station.")
        return settings.model_copy(update={"streams": streams})