"""live_rms.py — Live-RMS-Erkennung über einen ffmpeg-Decoder-Subprozess.

Startet einen ffmpeg-Prozess, der denselben Stream decodiert und die
Lautstärke (RMS) pro Sekunde über ``astats`` auf stderr ausgibt. Die
Werte werden geparst und mit Echtzeit-Zeitstempeln an einen
``RmsTracker`` weitergegeben — so erkennt der Recorder Songgrenzen
live, unabhängig von der ICY-Metadaten-Zuverlässigkeit.

Warum ffmpeg: Die Byte-Statistik komprimierter MP3-Daten korreliert nicht
zuverlässig mit der Lautstärke. Nur dekodiertes PCM liefert brauchbare
RMS-Werte.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from radio_ripper.silence import RmsTracker

_LOGGER = logging.getLogger("radio_ripper.live_rms")

_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-inf)")


class LiveRmsSource:
    """Startet ffmpeg und liefert live RMS-Werte an einen RmsTracker."""

    def __init__(
        self,
        stream_url: str,
        tracker: RmsTracker,
        *,
        user_agent: str = "VLC/3.0.18 LibVLC/3.0.18",
        request_timeout: float = 30.0,
    ) -> None:
        self._url = stream_url
        self._tracker = tracker
        self._user_agent = user_agent
        self._timeout = request_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._start_mono = 0.0
        self._last_rms: float | None = None

    def start(self) -> None:
        """Startet den ffmpeg-Prozess und den Lese-Task."""
        self._start_mono = time.monotonic()
        self._task = asyncio.create_task(self._run(), name="LiveRmsSource")

    async def stop(self) -> None:
        """Beendet den ffmpeg-Prozess und den Task."""
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.kill()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def current_rms(self) -> float | None:
        return self._last_rms

    async def _run(self) -> None:
        cmd = [
            "ffmpeg",
            "-v",
            "info",
            "-user_agent",
            self._user_agent,
            "-headers",
            "Icy-MetaData: 1",
            "-i",
            self._url,
            "-af",
            "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
            "-f",
            "null",
            "-",
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _LOGGER.warning("ffmpeg nicht gefunden — RMS-Erkennung deaktiviert.")
            return

        assert self._proc.stderr is not None
        buffer = b""
        while True:
            chunk = await self._proc.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk
            # Zeilen aus dem Puffer ziehen
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self._parse_line(line.decode("utf-8", errors="replace"))

    def _parse_line(self, line: str) -> None:
        m = _RMS_RE.search(line)
        if m is None:
            return
        raw = m.group(1)
        rms = -60.0 if raw == "-inf" else float(raw)
        self._last_rms = rms
        # Zeitstempel: Sekunden seit Prozessstart (Echtzeit)
        elapsed = time.monotonic() - self._start_mono
        self._tracker.add_rms(elapsed, rms)
