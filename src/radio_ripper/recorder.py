"""recorder.py — StreamRecorder: nimmt Radiostreams auf und validiert Tracks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from radio_ripper.acoustid import AcoustidWorker
from radio_ripper.config import Settings
from radio_ripper.http_client import HttpxClient, resolve_playlist
from radio_ripper.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.live_rms import LiveRmsSource
from radio_ripper.models import StreamConfig
from radio_ripper.silence import RmsTracker
from radio_ripper.validation import validate_file
from radio_ripper.writer import sanitize_filename

_LOGGER = logging.getLogger("radio_ripper.recorder")

_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0
_REQUEST_TIMEOUT = 30.0
_USER_AGENT = "VLC/3.0.18 LibVLC/3.0.18"
_NO_ICY_DISABLE_AFTER = 10

# Ring-Puffer: max 5 Minuten Audio (128kbps ≈ 4.8 MB) pro Sender
_RING_BUFFER_MAX_BYTES = 5 * 60 * 16_000


def cleanup_stale_parts(work_dir: Path) -> int:
    """Entfernt übrig gebliebene ``.part``-Dateien aus abgebrochenen Läufen.

    ``.part``-Dateien sind unvollständige Aufnahmen (der atomare Rename zu
    ``.mp3`` fand nie statt) und werden nie weiterverarbeitet.
    """
    staging = work_dir / "recordings"
    if not staging.is_dir():
        return 0
    parts = sorted(staging.glob("*.part"))
    for part in parts:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
    if parts:
        _LOGGER.info("Entfernt %d unvollständige Aufnahme(n) (.part) aus einem früheren Lauf.", len(parts))
    return len(parts)


class _SongBuffer:
    """Ring-Puffer für Audio-Bytes mit Byte-granularer Grenz-Suche.

    Der Stream schreibt fortlaufend in diesen Puffer. Die RmsTracker-Grenzen
    sind mit Byte-Offsets assoziiert (über die geschriebenen Bytes), sodass
    beim ICY-Wechsel der Song ab der letzten Grenze geschnitten werden kann —
    auch wenn der ICY-Titel verspätet kommt.
    """

    def __init__(self, max_bytes: int = _RING_BUFFER_MAX_BYTES) -> None:
        self._max_bytes = max_bytes
        self._data = bytearray()
        # Karte: Byte-Offset → (ist Grenze?)
        self._boundary_offsets: set[int] = set()

    def write(self, data: bytes) -> None:
        self._data.extend(data)
        if len(self._data) > self._max_bytes:
            # Älteste Bytes verwerfen; Boundary-Offsets anpassen
            overflow = len(self._data) - self._max_bytes
            del self._data[:overflow]
            self._boundary_offsets = {max(0, off - overflow) for off in self._boundary_offsets if off >= overflow}

    def mark_boundary(self, byte_offset: int) -> None:
        if 0 <= byte_offset <= len(self._data):
            self._boundary_offsets.add(byte_offset)

    def slice_from_last_boundary(self) -> bytes:
        """Gibt die Bytes ab der letzten Grenze zurück (oder den ganzen Puffer)."""
        if not self._boundary_offsets:
            return bytes(self._data)
        last = max(self._boundary_offsets)
        return bytes(self._data[last:])

    def slice_to(self, byte_offset: int) -> bytes:
        """Gibt die Bytes bis *byte_offset* ab der vorletzten Grenze zurück."""
        sorted_offs = sorted(o for o in self._boundary_offsets if o < byte_offset)
        start = sorted_offs[-1] if sorted_offs else 0
        return bytes(self._data[start:byte_offset])

    def clear(self) -> None:
        self._data.clear()
        self._boundary_offsets.clear()

    @property
    def size(self) -> int:
        return len(self._data)


class StreamRecorder:
    """Nimmt einen einzelnen Radiostream auf und validiert jeden Track."""

    def __init__(
        self,
        *,
        station: StreamConfig,
        settings: Settings,
        client: HttpxClient,
        executor: ThreadPoolExecutor,
        acoustid_worker: AcoustidWorker | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.station = station
        self.settings = settings
        self._client = client
        self._executor = executor
        self._acoustid_worker = acoustid_worker
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._no_icy_failures = 0
        self._connect_failures = 0

    @property
    def station_name(self) -> str:
        return self.station.name

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        self._stop_event.set()

    async def join(self) -> None:
        if self._task is not None:
            await self._task

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self._run_forever(), name=f"Recorder-{self.station.name}")
        return self._task

    # ------------------------------------------------------------------ core loop

    async def _run_forever(self) -> None:
        self._log.info(
            "Starte Recorder '%s' für '%s'",
            self.station.name,
            self.station.url,
        )
        delay = _RECONNECT_BASE_DELAY
        while not self._stop_event.is_set():
            try:
                ok = await self._run_once()
            except Exception:
                self._log.exception("Unerwarteter Fehler in Recorder '%s'", self.station.name)
                ok = False
            if self._stop_event.is_set():
                break
            if self._no_icy_failures >= _NO_ICY_DISABLE_AFTER:
                self._log.error(
                    "[%s] Deaktiviert: kein ICY-Metadaten nach %d Versuchen. Stream unterstützt vermutlich kein ICY.",
                    self.station.name,
                    self._no_icy_failures,
                )
                break
            if self._connect_failures >= _NO_ICY_DISABLE_AFTER:
                self._log.error(
                    "[%s] Deaktiviert: %d Verbindungsfehler in Folge.",
                    self.station.name,
                    self._connect_failures,
                )
                break
            if ok:
                delay = _RECONNECT_BASE_DELAY
            else:
                self._log.info(
                    "[%s] Reconnect in %.1fs (max %.1fs)",
                    self.station.name,
                    delay,
                    _RECONNECT_MAX_DELAY,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                delay = min(delay * 2.0, _RECONNECT_MAX_DELAY)
                delay *= 1.0 + random.random() * 0.5  # noqa: S311  -- Jitter gegen Thundering-Herd
        self._log.info("Recorder '%s' gestoppt.", self.station.name)

    async def _run_once(self) -> bool:
        try:
            urls = await resolve_playlist(
                self._client,
                str(self.station.url),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            self._log.error("[%s] Playlist-Fehler: %s", self.station.name, exc)
            self._connect_failures += 1
            return False
        if not urls:
            self._log.error("[%s] Playlist enthielt keine Stream-URLs.", self.station.name)
            return False
        stream_url = urls[0]
        self._log.info("[%s] Verwende Stream-URL: %s", self.station.name, stream_url)
        try:
            ok = await self._stream_with_meta(stream_url)
            self._connect_failures = 0
            return ok
        except httpx.TimeoutException:
            self._log.error("[%s] Timeout beim Verbinden.", self.station.name)
            self._connect_failures += 1
            return False
        except httpx.HTTPError as exc:
            self._log.error("[%s] HTTP-Fehler: %s", self.station.name, exc)
            self._connect_failures += 1
            return False

    # ------------------------------------------------------------------ stream helpers

    async def _connect_stream(self, stream_url: str) -> tuple[AsyncGenerator[bytes, None], IcyParser] | None:
        headers = {"Icy-MetaData": "1"}
        agen = self._client.stream_binary(
            stream_url,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        try:
            first_chunk = await agen.__anext__()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await agen.aclose()
            self._log.warning(
                "[%s] Verbindung fehlgeschlagen: %s: %r",
                self.station.name,
                type(exc).__name__,
                exc,
            )
            raise
        resp_headers = self._client.response_headers()
        try:
            metaint = int(resp_headers.get("icy-metaint", 0))
        except (TypeError, ValueError):
            metaint = 0
        if not metaint or metaint <= 0:
            self._no_icy_failures += 1
            self._log.info(
                "[%s] Kein icy-metaint-Header; schließe. (Fehler %d/%d)",
                self.station.name,
                self._no_icy_failures,
                _NO_ICY_DISABLE_AFTER,
            )
            with contextlib.suppress(Exception):
                await agen.aclose()
            return None
        self._no_icy_failures = 0
        self._log.info("[%s] icy-metaint=%d", self.station.name, metaint)
        parser = IcyParser(metaint)
        parser.feed(first_chunk or b"")
        return agen, parser

    # ------------------------------------------------------------------ main stream loop

    async def _stream_with_meta(self, stream_url: str) -> bool:
        connected = await self._connect_stream(stream_url)
        if connected is None:
            return False
        agen, parser = connected

        # Live-RMS-Erkennung (echte PCM-Werte über separaten ffmpeg-Prozess)
        tracker = RmsTracker()
        rms_source = LiveRmsSource(
            stream_url,
            tracker,
            user_agent=_USER_AGENT,
            request_timeout=_REQUEST_TIMEOUT,
        )
        rms_source.start()
        try:
            return await self._stream_loop(agen, parser, tracker)
        finally:
            await rms_source.stop()

    async def _stream_loop(
        self,
        agen: AsyncGenerator[bytes, None],
        parser: IcyParser,
        tracker: RmsTracker,
    ) -> bool:
        import time as _time

        buffer = _SongBuffer()
        first_title_seen: str | None = None
        current_title: str | None = None
        recording = False
        pending_title: str | None = None  # Titel des aktuell aufzubauenden Songs
        start_mono = _time.monotonic()

        try:
            async for chunk in agen:
                if self._stop_event.is_set():
                    return True
                if not chunk:
                    continue
                parser.feed(chunk)
                for event in parser.events():
                    if isinstance(event, AudioChunk):
                        buffer.write(event.data)
                        # Grenze aus RMS-Tracker an Byte-Position markieren
                        self._sync_boundaries(buffer, tracker, start_mono)
                        if recording and pending_title:
                            pass  # Audio wird beim ICY-Wechsel aus dem Puffer geschnitten
                    elif isinstance(event, TitleChanged):
                        new_title = event.title
                        if first_title_seen is None:
                            first_title_seen = new_title
                            current_title = new_title
                            self._log.info(
                                "[%s] Mitten im Song '%s' eingestiegen — warte auf nächste Grenze.",
                                self.station.name,
                                new_title,
                            )
                            continue
                        if new_title == current_title:
                            continue
                        # Neuer Titel: vorherigen Song aus Puffer schneiden + committen
                        if pending_title:
                            await self._commit_buffered_song(buffer, pending_title)
                        current_title = new_title
                        pending_title = new_title.strip()
                        recording = True
                        self._log.info("[%s] Songwechsel -> %s", self.station.name, new_title)
            self._log.info("[%s] Stream beendet (EOF).", self.station.name)
            if pending_title:
                await self._commit_buffered_song(buffer, pending_title)
            return True
        except Exception as exc:
            self._log.warning("[%s] Stream unterbrochen: %s", self.station.name, exc)
            return False
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    def _sync_boundaries(self, buffer: _SongBuffer, tracker: RmsTracker, start_mono: float) -> None:
        """Markiert neue Grenzen im Puffer (Zeit → Byte-Position)."""
        import time as _time

        elapsed = _time.monotonic() - start_mono
        # Bits/s des Streams: aus 128kbps Standard schätzen (16 KB/s)
        bps = 16_000
        byte_pos = int(elapsed * bps)
        for b in tracker.boundaries:
            # Letzte erkannte Grenze (im Tracker nach Zeit)
            b_pos = int(b.time * bps)
            if b_pos <= byte_pos:
                buffer.mark_boundary(b_pos)
        # Nur die letzte Grenze zählt für den Songstart — wir halten alle

    async def _commit_buffered_song(self, buffer: _SongBuffer, title: str) -> None:
        """Schneidet den aktuellen Song ab der letzten Grenze, committet ihn."""
        data = buffer.slice_from_last_boundary()
        if not data:
            self._log.info("[%s] Kein Audio für '%s'", self.station.name, title)
            return
        safe_name = sanitize_filename(title)
        if not safe_name:
            self._log.error("[%s] Kein Dateiname für Titel=%r", self.station.name, title)
            return
        recordings_dir = self.settings.work_dir / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        final_path = recordings_dir / f"{safe_name}.mp3"
        try:
            with open(final_path, "xb") as fh:
                fh.write(data)
        except FileExistsError:
            # Kollision: UUID anhängen
            import uuid

            final_path = recordings_dir / f"{safe_name}.{uuid.uuid4().hex}.mp3"
            with open(final_path, "xb") as fh:
                fh.write(data)
        except OSError as exc:
            self._log.error("[%s] Konnte %s nicht schreiben: %s", self.station.name, final_path, exc)
            return

        ok = await validate_file(final_path, self.settings, self._executor)
        if not ok:
            return
        self._log.info(
            "[%s] Song '%s' gespeichert (%d bytes)",
            self.station.name,
            title,
            final_path.stat().st_size,
        )
        if self._acoustid_worker is not None:
            self._acoustid_worker.enqueue(final_path)
