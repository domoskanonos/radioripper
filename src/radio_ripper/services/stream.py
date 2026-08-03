"""Stream recorder — records ICY streams into unchecked_mp3 staging.

The recorder is responsible for:
  1. Connecting to a stream URL and parsing ICY metadata.
  2. Writing audio chunks to a temporary ``.part`` file inside ``unchecked_mp3/``.
  3. At each title boundary: committing the file (size + duration + MP3 validity
     checks), then handing it to the ``AcoustidQueue`` for async fingerprinting.

The recorder does *not* do AcoustID lookups itself any more.  All final naming
and destination placement is handled by ``AcoustidQueue``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import InvalidUrlError, StreamConnectionError, StreamProtocolError
from radio_ripper.infra.validation import validate_stream_url
from radio_ripper.services.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.services.playlist import PlaylistResolver
from radio_ripper.services.storage import (
    TrackWriter,
    get_mp3_duration,
    is_valid_mp3,
    sanitize_filename,
)

if TYPE_CHECKING:
    from radio_ripper.infra.http import AsyncHttpClient
    from radio_ripper.services.acoustid_queue import AcoustidQueue

_LOGGER = logging.getLogger(__name__)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _parse_metaint(headers: dict[str, str]) -> int | None:
    for key in ("icy-metaint", "Icy-Metaint", "ICY-METAINT"):
        val = headers.get(key)
        if val:
            try:
                return int(val)
            except ValueError:
                return None
    return None


class StreamRecorder:
    def __init__(
        self,
        *,
        station_name: str,
        playlist_url: str,
        settings: Settings,
        http_client: AsyncHttpClient,
        playlist_resolver: PlaylistResolver,
        acoustid_queue: AcoustidQueue | None = None,
        logger: logging.Logger | None = None,
        ignore_title_patterns: list[str] | None = None,
        no_icy_disable_after: int = 10,
        station_bitrate: int = 0,
    ) -> None:
        self.station_name = station_name
        try:
            self.playlist_url = validate_stream_url(playlist_url)
        except InvalidUrlError as e:
            raise ValueError(f"Invalid playlist URL for station '{station_name}': {e}") from e
        self.settings = settings
        self._http = http_client
        self._resolver = playlist_resolver
        self._acoustid_queue = acoustid_queue
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        pats = ignore_title_patterns or []
        self._ignore_patterns: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in pats]
        self._no_icy_disable_after = no_icy_disable_after
        self._no_icy_failures = 0
        self._connect_failures = 0
        self._paused = asyncio.Event()
        self._station_bitrate = station_bitrate

    # ------------------------------------------------------------------ lifecycle

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def _is_ignored_title(self, title: str) -> bool:
        return bool(self._ignore_patterns and any(p.search(title) for p in self._ignore_patterns))

    def stop(self) -> None:
        self._stop_event.set()

    async def join(self) -> None:
        if self._task is not None:
            await self._task

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self._run_forever(), name=f"Recorder-{self.station_name}")
        return self._task

    # ------------------------------------------------------------------ core loop

    async def _run_forever(self) -> None:
        self._log.info(
            "Starting recorder '%s' for playlist '%s'",
            self.station_name,
            self.playlist_url,
        )
        delay = self.settings.reconnect_base_delay
        while not self._stop_event.is_set():
            if self._paused.is_set():
                self._log.info("[%s] Paused — waiting for resume.", self.station_name)
                while not self._stop_event.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            asyncio.gather(self._paused.wait(), self._stop_event.wait()),
                            timeout=5,
                        )
                    if not self._paused.is_set():
                        break
                if self._stop_event.is_set():
                    break
                self._log.info("[%s] Resumed.", self.station_name)
                delay = self.settings.reconnect_base_delay
            try:
                ok = await self._run_once()
            except Exception:
                self._log.exception("Uncaught error in recorder '%s'", self.station_name)
                ok = False
            if self._stop_event.is_set():
                break
            if self._no_icy_failures >= self._no_icy_disable_after:
                self._log.error(
                    "[%s] Disabled: no ICY metadata after %d consecutive attempts. "
                    "Stream likely does not support ICY or always plays ads.",
                    self.station_name,
                    self._no_icy_failures,
                )
                break
            if self._connect_failures >= self._no_icy_disable_after:
                self._log.error(
                    "[%s] Disabled: connect failed %d times in a row. Removing station from active set.",
                    self.station_name,
                    self._connect_failures,
                )
                break
            if ok:
                delay = self.settings.reconnect_base_delay
            else:
                self._log.info(
                    "[%s] Reconnect in %.1fs (max %.1fs)",
                    self.station_name,
                    delay,
                    self.settings.reconnect_max_delay,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                delay = min(delay * 2.0, self.settings.reconnect_max_delay)
                delay *= 1.0 + random.random() * 0.1  # noqa: S311  -- jitter, not cryptographic
        self._log.info("Recorder '%s' stopped.", self.station_name)

    async def _run_once(self) -> bool:
        urls = await self._resolver.resolve(self.playlist_url)
        if not urls:
            self._log.error("[%s] Playlist contained no stream URLs.", self.station_name)
            return False
        stream_url = urls[0]
        self._log.info("[%s] Using stream URL: %s", self.station_name, stream_url)
        try:
            ok = await self._stream_with_meta(stream_url)
            self._connect_failures = 0
            return ok
        except StreamConnectionError as exc:
            self._log.error("[%s] Request failed: %s", self.station_name, exc)
            self._connect_failures += 1
            return False
        except StreamProtocolError as exc:
            self._log.warning("[%s] Protocol error: %s", self.station_name, exc)
            self._connect_failures = 0
            return False

    # ------------------------------------------------------------------ stream helpers

    async def _connect_stream(self, stream_url: str) -> tuple[Any, IcyParser] | None:
        headers = {"Icy-MetaData": "1"}
        agen = self._http.stream_binary(
            stream_url,
            headers=headers,
            timeout=self.settings.request_timeout,
        )
        try:
            first_chunk = await agen.__anext__()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await agen.aclose()
            self._log.warning("[%s] connect failed: %s: %r", self.station_name, type(exc).__name__, exc)
            raise StreamConnectionError(f"connect failed: {type(exc).__name__}: {exc!r}") from exc
        resp_headers = self._http.response_headers()
        metaint = _parse_metaint(resp_headers)
        if not metaint or metaint <= 0:
            self._no_icy_failures += 1
            self._log.info(
                "[%s] No icy-metaint header; closing. (failure %d/%d)",
                self.station_name,
                self._no_icy_failures,
                self._no_icy_disable_after,
            )
            with contextlib.suppress(Exception):
                await agen.aclose()
            return None
        self._no_icy_failures = 0
        self._log.info("[%s] icy-metaint=%d", self.station_name, metaint)
        parser = IcyParser(metaint)
        parser.feed(first_chunk or b"")
        return agen, parser

    async def _check_min_duration(self, path: Path) -> bool:
        min_dur = self.settings.min_file_duration_s
        if min_dur <= 0:
            return True
        dur = await get_mp3_duration(path)
        if dur is None:
            return True
        if dur >= min_dur:
            return True
        self._log.info(
            "[%s] Discarded (too short: %.1fs < %.0fs): %s",
            self.station_name,
            dur,
            min_dur,
            path.name,
        )
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return False

    def _make_writer(self, icy_title: str) -> TrackWriter | None:
        """Create a TrackWriter that stages the recording in unchecked_mp3/.

        The filename is ``<safe_icy_title>.<uuid>.mp3`` so multiple recordings
        of the same title can coexist without clobbering each other.
        The ICY title is only used as a human-readable hint — the final filename
        will be determined by the AcoustID lookup.
        """
        unchecked_dir = self.settings.work_dir / "unchecked_mp3"
        unchecked_dir.mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_filename(icy_title)
        if not safe_name:
            self._log.error("[%s] Cannot create file for title=%r", self.station_name, icy_title)
            return None

        # Unique staging name: keeps ICY title readable while avoiding collisions
        file_path = unchecked_dir / f"{safe_name}.{uuid.uuid4().hex}.mp3"
        try:
            return TrackWriter(
                file_path,
                min_size=self.settings.min_file_size_bytes,
            )
        except OSError as exc:
            self._log.error("[%s] cannot open %s: %s", self.station_name, file_path, exc)
            return None

    def _should_record_title(self, title: str) -> bool:
        clean = title.strip()
        if not clean:
            self._log.info("[%s] Blank title, skipping", self.station_name)
            return False
        if self._is_ignored_title(clean):
            self._log.info("[%s] Ad title detected, skipping: %s", self.station_name, clean)
            return False
        return True

    # ------------------------------------------------------------------ main stream loop

    async def _stream_with_meta(self, stream_url: str) -> bool:
        connected = await self._connect_stream(stream_url)
        if connected is None:
            return False
        agen, parser = connected

        first_title_seen: str | None = None
        current_title: str | None = None
        writer: TrackWriter | None = None
        recording = False

        try:
            async for chunk in agen:
                if self._stop_event.is_set():
                    self._log.info("[%s] Stop requested; discarding in-flight song.", self.station_name)
                    if writer is not None:
                        writer.discard()
                    return True
                if not chunk:
                    continue
                parser.feed(chunk)
                for event in parser.events():
                    if isinstance(event, AudioChunk):
                        if recording and writer is not None:
                            writer.write(event.data)
                    elif isinstance(event, TitleChanged):
                        new_title = event.title
                        if first_title_seen is None:
                            first_title_seen = new_title
                            current_title = new_title
                            self._log.info(
                                "[%s] Joined mid-song '%s' - waiting for next boundary.",
                                self.station_name,
                                new_title,
                            )
                            continue
                        if new_title == current_title:
                            continue
                        # Title change
                        if recording and writer is not None:
                            await self._finalize_writer(writer, current_title)
                            writer = None
                            recording = False
                        current_title = new_title
                        if not self._should_record_title(new_title):
                            continue
                        writer = self._make_writer(new_title.strip())
                        if writer is not None:
                            recording = True
                            self._log.info(
                                "[%s] Recording -> %s",
                                self.station_name,
                                writer.final_path.name,
                            )
                        else:
                            recording = False
            self._log.info("[%s] stream ended (EOF).", self.station_name)
            if writer is not None:
                writer.discard()
            return True
        except Exception as exc:
            self._log.warning("[%s] stream interrupted: %s", self.station_name, exc)
            if writer is not None:
                writer.discard()
            return False
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    async def _finalize_writer(self, writer: TrackWriter, _icy_title: str | None = None) -> None:
        """Commit a completed recording, run quality checks, hand off to queue."""
        committed = writer.commit()
        if not committed:
            self._log.info(
                "[%s] Discarded (too small): %s",
                self.station_name,
                writer.final_path.name,
            )
            return

        final_path = writer.final_path

        if 0 < self._station_bitrate < 128:
            self._log.info(
                "[%s] Discarded (bitrate %d kbps < 128): %s",
                self.station_name,
                self._station_bitrate,
                final_path.name,
            )
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            return

        if not await is_valid_mp3(final_path):
            self._log.info(
                "[%s] Discarded (not a valid MP3): %s",
                self.station_name,
                final_path.name,
            )
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            return

        ok = await self._check_min_duration(final_path)
        if not ok:
            return

        # All local checks passed — hand off to AcoustID queue
        if self._acoustid_queue is not None:
            self._log.info(
                "[%s] Queued for AcoustID: %s (%d bytes)",
                self.station_name,
                final_path.name,
                _safe_size(final_path),
            )
            self._acoustid_queue.enqueue(final_path)
        else:
            # No queue configured (tests / no API key): keep as-is in unchecked_mp3
            self._log.info(
                "[%s] No AcoustID queue — file stays in unchecked_mp3: %s (%d bytes)",
                self.station_name,
                final_path.name,
                _safe_size(final_path),
            )


__all__ = ["StreamRecorder"]
