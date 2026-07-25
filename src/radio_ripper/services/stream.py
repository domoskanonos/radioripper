from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import StreamConnectionError, StreamProtocolError
from radio_ripper.services.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.services.playlist import PlaylistResolver
from radio_ripper.services.storage import TrackWriter, get_mp3_duration, sanitize_filename

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
        http_client: Any,
        playlist_resolver: PlaylistResolver,
        logger: logging.Logger | None = None,
        ad_title_patterns: list[str] | None = None,
        no_icy_disable_after: int = 10,
        startup_grace_titles: int = 2,
    ) -> None:
        self.station_name = station_name
        self.playlist_url = playlist_url
        self.settings = settings
        self._http = http_client
        self._resolver = playlist_resolver
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ad_patterns: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in (ad_title_patterns or [])]
        self._no_icy_disable_after = no_icy_disable_after
        self._no_icy_failures = 0
        self._connect_failures = 0
        self._startup_grace_titles = startup_grace_titles

    # ------------------------------------------------------------------ lifecycle

    def _is_ad_title(self, title: str) -> bool:
        return bool(self._ad_patterns and any(p.search(title) for p in self._ad_patterns))

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
            raise StreamConnectionError(f"connect failed: {exc}") from exc
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
        min_dur = self.settings.min_duration_s
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

    def _make_writer(self, title: str) -> TrackWriter | None:
        stream_dir = self.settings.mp3_inbox or self.settings.work_dir / "mp3_inbox"
        stream_dir.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(title)
        if not safe_name:
            self._log.error("[%s] Cannot create file for title=%r", self.station_name, title)
            return None
        file_path = stream_dir / (safe_name + ".mp3")
        try:
            return TrackWriter(file_path, min_size=self.settings.min_file_size_bytes)
        except OSError as exc:
            self._log.error("[%s] cannot open %s: %s", self.station_name, file_path, exc)
            return None

    def _should_record_title(self, title: str) -> bool:
        clean = title.strip()
        if not clean:
            self._log.info("[%s] Blank title, skipping", self.station_name)
            return False
        if self._is_ad_title(clean):
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
        grace_remaining = self._startup_grace_titles

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
                        if grace_remaining > 0:
                            grace_remaining -= 1
                            self._log.info(
                                "[%s] Grace period: skipping '%s' (%d remaining)",
                                self.station_name,
                                new_title.strip(),
                                grace_remaining,
                            )
                            continue
                        writer = self._make_writer(new_title.strip())
                        if writer is not None:
                            recording = True
                            self._log.info("[%s] Recording -> %s", self.station_name, writer.final_path.name)
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

    async def _finalize_writer(self, writer: TrackWriter, _current_title: str | None = None) -> None:
        committed = writer.commit()
        if not committed:
            self._log.info(
                "[%s] Discarded (too small): %s",
                self.station_name,
                writer.final_path.name,
            )
            return
        final_path = writer.final_path
        ok = await self._check_min_duration(final_path)
        if ok:
            self._log.info(
                "[%s] Streaming result: %s (%d bytes)",
                self.station_name,
                final_path.name,
                _safe_size(final_path),
            )


__all__ = ["StreamRecorder"]
