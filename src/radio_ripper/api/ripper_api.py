"""Ripper API — start/stop the ripper from a synchronous (Gradio) context.

The ripper runs in a **background thread** with its own asyncio event loop.
This avoids blocking the Gradio UI thread and allows clean shutdown via
``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from enum import Enum

from radio_ripper.app import RadioRipperApp
from radio_ripper.infra.config import Settings
from radio_ripper.infra.logging import configure_logging

__all__ = ["RipperApi", "RipperStatus"]


class RipperStatus(Enum):
    """Lifecycle state of the background ripper."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class RipperApi:
    """Manage the ripper lifecycle from synchronous GUI callbacks."""

    def __init__(self, config_path: str | None = None) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: RadioRipperApp | None = None
        self._stop_event: asyncio.Event | None = None
        self._status = RipperStatus.STOPPED
        self._config_path = config_path
        self._lock = threading.Lock()
        self._settings: Settings | None = None

    @property
    def status(self) -> RipperStatus:
        return self._status

    def start(self, settings: Settings) -> str:
        """Start the ripper in a background thread. Returns a status message."""
        with self._lock:
            if self._status in (RipperStatus.RUNNING, RipperStatus.STARTING):
                return "Ripper läuft bereits."
            self._status = RipperStatus.STARTING
            self._settings = settings
            self._thread = threading.Thread(
                target=self._run_thread,
                name="ripper-bg",
                daemon=True,
            )
            self._thread.start()
            return "Ripper wird gestartet…"

    def stop(self) -> str:
        """Signal the ripper to stop gracefully. Returns a status message."""
        with self._lock:
            if self._status not in (RipperStatus.RUNNING, RipperStatus.STARTING):
                return "Ripper läuft nicht."
            self._status = RipperStatus.STOPPING
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=15.0)
        with self._lock:
            self._status = RipperStatus.STOPPED
            self._thread = None
            self._loop = None
            self._app = None
            self._stop_event = None
        return "Ripper gestoppt."

    def _run_thread(self) -> None:
        """Background-thread entry point: create event loop and run the ripper."""
        try:
            asyncio.run(self._run_async())
        except Exception:
            logging.getLogger(__name__).exception("Ripper background thread crashed.")
        finally:
            with self._lock:
                self._status = RipperStatus.STOPPED

    async def _run_async(self) -> None:
        """Set up the event loop, start the app, wait for stop signal."""
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        logger = configure_logging(self._settings.log_level, self._settings.log_file)
        logger.info("=== Radio-Ripper GUI background mode starting ===")

        self._app = RadioRipperApp.from_settings(self._settings, logger=logger)
        await self._app.start()

        with self._lock:
            self._status = RipperStatus.RUNNING
        logger.info("Ripper is running (started from GUI).")

        await self._stop_event.wait()
        logger.info("Stop signal received — shutting down ripper…")
        await self._app.stop()
        logger.info("Ripper stopped cleanly.")