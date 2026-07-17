"""Backend API layer for the Gradio GUI.

This package provides a synchronous façade over the async backend services
so that Gradio callback functions (which are synchronous) can interact with
the ripper without dealing with ``asyncio`` directly.

Modules:
    - :mod:`config_api`  — load/save/edit :class:`Settings`
    - :mod:`station_api` — station CRUD on top of ``Settings.streams``
    - :mod:`library_api` — query recorded songs from SQLite + filesystem
    - :mod:`ripper_api`  — start/stop the ripper in a background asyncio loop
"""

from __future__ import annotations

from radio_ripper.api.config_api import ConfigApi
from radio_ripper.api.library_api import LibraryApi
from radio_ripper.api.ripper_api import RipperApi
from radio_ripper.api.station_api import StationApi

__all__ = ["ConfigApi", "LibraryApi", "RipperApi", "StationApi"]
