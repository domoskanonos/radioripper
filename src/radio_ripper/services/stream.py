"""Async stream recorder.

One :class:`StreamRecorder` coroutine per station. Connects via the
:class:`~radio_ripper.infra.http.AsyncHttpClient` ABC, drives the pure
:class:`~radio_ripper.services.icy.IcyParser` state machine, and delegates file
IO to :class:`~radio_ripper.services.storage.TrackWriter`,tagging to a
:class:`~radio_ripper.services.tagging.TrackTagger`, and dedup/registration to
a :class:`~radio_ripper.services.repository.TrackRepository`.

Behaviour preserved from v1.x:
    * Only *complete* songs are saved. The first running song at join is
      discarded and recording starts at the *next* title boundary.
    * If interrupted mid-song the in-flight temp file is discarded.
    * Exponential reconnect backoff (doubles, capped at ``max_delay``).
    * Dupes are skipped via the repository ``exists`` check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import EnrichedInfo, SavedTrack, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import StreamConnectionError, StreamProtocolError
from radio_ripper.services.fingerprint import FingerprintProvider
from radio_ripper.services.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.playlist import PlaylistResolver
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import (
    TrackWriter,
    compute_file_path,
    get_mp3_duration,
    remove_empty_parents,
    remux_mp3,
    trim_trailing,
)
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.track_processing import (
    enrich_song,
    fingerprint_song,
    register_and_enrich,
)

_LOGGER = logging.getLogger("radio_ripper.stream")


class StreamRecorder:
    """Manage the perpetual recording loop for a single station."""

    def __init__(
        self,
        *,
        station_name: str,
        playlist_url: str,
        settings: Settings,
        http_client: Any,
        playlist_resolver: PlaylistResolver,
        repository: TrackRepository,
        tagger: TrackTagger,
        metadata_provider: MetadataProvider | None = None,
        fingerprint_provider: FingerprintProvider | None = None,
        cover_provider: Any | None = None,
        popularity_provider: Any | None = None,
        enrich_semaphore: asyncio.Semaphore | None = None,
        logger: logging.Logger | None = None,
        ad_title_patterns: list[str] | None = None,
        no_icy_disable_after: int = 10,
    ) -> None:
        self.station_name = station_name
        self.playlist_url = playlist_url
        self.settings = settings
        self._http = http_client
        self._resolver = playlist_resolver
        self._repo = repository
        self._tagger = tagger
        self._metadata = metadata_provider
        self._fingerprint = fingerprint_provider
        self._cover_provider = cover_provider
        self._popularity = popularity_provider
        self._enrich_sem = enrich_semaphore
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._enrichment_tasks: set[asyncio.Task[Any]] = set()
        self._ad_patterns: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in (ad_title_patterns or [])
        ]
        self._no_icy_disable_after = no_icy_disable_after
        self._no_icy_failures = 0
        self._connect_failures = 0
        # Per-file locks: serialize enrichment vs fingerprinting on the same path
        # so rename (in _fingerprint_song) doesn't race with write_full (in _enrich_song).
        self._file_locks: dict[Path, asyncio.Lock] = {}
        self._last_limit_log = 0.0
        # Rolling audio buffer: captures ~1 s of audio to recover song beginnings
        # lost to ICY metadata-interval latency (audio arrives before StreamTitle).
        self._audio_buffer: bytearray = bytearray()
        self._max_buffer: int = 16384

    def _lock_for(self, path: Path) -> asyncio.Lock:
        """Get (or create) the asyncio.Lock for *path*."""
        lock = self._file_locks.get(path)
        if lock is None:
            lock = asyncio.Lock()
            self._file_locks[path] = lock
        return lock

    def _release_lock(self, path: Path) -> None:
        """Remove the per-file lock after the terminal operation completed."""
        self._file_locks.pop(path, None)

    # ------------------------------------------------------------------ lifecycle

    def _is_ad_title(self, title: str) -> bool:
        """Return True if *title* matches any configured ad-title pattern."""
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
                    "[%s] Disabled: connect failed %d times in a row. "
                    "Removing station from active set.",
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

    async def _stream_with_meta(self, stream_url: str) -> bool:
        """Drive the IcyParser state machine against the live HTTP stream."""
        headers = {"Icy-MetaData": "1"}
        first_chunk: bytes | None = None
        try:
            agen = self._http.stream_binary(
                stream_url,
                headers=headers,
                timeout=self.settings.request_timeout,
            )
            first_chunk = await agen.__anext__()  # warm up so headers are available
        except Exception as exc:
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
            return False
        self._no_icy_failures = 0  # reset on successful ICY connection
        self._log.info("[%s] icy-metaint=%d", self.station_name, metaint)
        self._max_buffer = min(metaint, 24576)
        parser = IcyParser(metaint)

        first_title_seen: str | None = None
        current_title: str | None = None
        writer: TrackWriter | None = None
        recording = False

        async def _close_writer(finalize: bool) -> None:
            nonlocal writer, current_title, recording
            if writer is None:
                return
            if finalize:
                committed = writer.commit()
                if not committed:
                    self._log.info(
                        "[%s] Discarded (too small): %s", self.station_name, writer.final_path.name
                    )
                    remove_empty_parents(writer.final_path, self.settings.destination)
                    current_title = None
                    recording = False
                    writer = None
                    return
                final_path = writer.final_path
                saved_title = current_title or ""

                # Reset state now — the recorder can start the next song immediately
                writer = None
                current_title = None
                recording = False

                # Quick post-processing in thread pool (non-blocking for event loop)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, remux_mp3, final_path)
                await loop.run_in_executor(None, trim_trailing, final_path)

                min_dur = self.settings.min_duration_s
                if min_dur > 0:
                    dur = await loop.run_in_executor(None, get_mp3_duration, final_path)
                    if dur is not None and dur < min_dur:
                        self._log.info(
                            "[%s] Discarded (too short: %.1fs < %.0fs): %s",
                            self.station_name,
                            dur,
                            min_dur,
                            final_path.name,
                        )
                        with contextlib.suppress(OSError):
                            final_path.unlink(missing_ok=True)
                        remove_empty_parents(final_path, self.settings.destination)
                        return

                track = TrackInfo.from_stream_title(saved_title)
                provenance = f"{self.station_name}@{self.playlist_url}"

                # Offload enrichment + fingerprinting to background task
                async def _post_process(fp: Path, trk: TrackInfo, prov: str) -> None:
                    enriched_path = await register_and_enrich(
                        fp,
                        trk,
                        self.station_name,
                        prov,
                        self.settings,
                        self._repo,
                        self._tagger,
                        metadata_provider=self._metadata,
                        enrich_semaphore=self._enrich_sem,
                        file_locks=self._file_locks,
                        logger=self._log,
                    )
                    if enriched_path is None:
                        return
                    if self._fingerprint is not None:
                        await fingerprint_song(
                            enriched_path,
                            trk,
                            self.station_name,
                            prov,
                            self.settings,
                            self._fingerprint,
                            self._repo,
                            self._tagger,
                            cover_provider=self._cover_provider,
                            popularity_provider=self._popularity,
                            file_locks=self._file_locks,
                            logger=self._log,
                        )
                    # Fetch & write lyrics
                    try:
                        from radio_ripper.services.lyrics import LyricsOvhProvider

                        lyrics_provider = LyricsOvhProvider(self._http, timeout=5.0)
                        lyrics = await lyrics_provider.fetch(trk.artist, trk.title)
                        if lyrics:
                            self._tagger.write_lyrics(enriched_path, lyrics)
                            self._log.info(
                                "[%s] Lyrics found for %s (%d chars)",
                                self.station_name,
                                enriched_path.name,
                                len(lyrics),
                            )
                    except Exception:
                        self._log.debug(
                            "[%s] Lyrics fetch failed for %s", self.station_name, enriched_path.name
                        )

                task = asyncio.create_task(_post_process(final_path, track, provenance))
                self._enrichment_tasks.add(task)
                task.add_done_callback(self._enrichment_tasks.discard)
            else:
                writer.discard()
                self._log.info(
                    "[%s] Discarded incomplete: %s (%d bytes)",
                    self.station_name,
                    writer.final_path.name,
                    writer.size,
                )
                remove_empty_parents(writer.final_path, self.settings.destination)
                writer = None
                current_title = None
                recording = False

        try:
            # First chunk already pulled above; feed it
            parser.feed(first_chunk or b"")
            async for chunk in agen:
                if self._stop_event.is_set():
                    self._log.info(
                        "[%s] Stop requested; discarding in-flight song.",
                        self.station_name,
                    )
                    await _close_writer(finalize=False)
                    return True
                if not chunk:
                    continue
                parser.feed(chunk)
                for event in parser.events():
                    if isinstance(event, AudioChunk):
                        # Rolling buffer für Song-Anfänge (ICY-Latenz)
                        self._audio_buffer.extend(event.data)
                        if len(self._audio_buffer) > self._max_buffer:
                            excess = len(self._audio_buffer) - self._max_buffer
                            del self._audio_buffer[:excess]
                        if recording and writer is not None:
                            writer.write(event.data)
                        # else: phase 1 / duplicate mode -> discard bytes
                    elif isinstance(event, TitleChanged):
                        new_title = event.title
                        if first_title_seen is None:
                            first_title_seen = new_title
                            current_title = new_title
                            self._audio_buffer.clear()
                            self._log.info(
                                "[%s] Joined mid-song '%s' - waiting for next boundary.",
                                self.station_name,
                                new_title,
                            )
                            continue
                        if new_title == current_title:
                            continue
                        # ---- Song-Wechsel ----
                        if recording and writer is not None:
                            await _close_writer(finalize=True)
                        current_title = new_title
                        clean = new_title.strip()
                        if not clean:
                            self._audio_buffer.clear()
                            recording = False
                            continue
                        if self._is_ad_title(clean):
                            self._log.info(
                                "[%s] Ad title detected, skipping: %s",
                                self.station_name,
                                clean,
                            )
                            self._audio_buffer.clear()
                            recording = False
                            continue
                        try:
                            if await self._repo.exists(self.station_name, clean):
                                self._log.info(
                                    "[%s] Skipping duplicate (already in DB): %s",
                                    self.station_name,
                                    clean,
                                )
                                self._audio_buffer.clear()
                                recording = False
                                continue
                        except Exception:
                            self._log.exception(
                                "[%s] repo.exists failed for: %s",
                                self.station_name,
                                clean,
                            )
                        track = TrackInfo.from_stream_title(clean)
                        file_path = compute_file_path(
                            self.settings.destination,
                            track.artist,
                            track.title,
                            clean,
                            overwrite=self.settings.overwrite_existing_files,
                        )
                        # Write as .untested.mp3 until AcoustID confirms the match
                        file_path = file_path.with_name(
                            file_path.stem + ".untested" + file_path.suffix
                        )
                        if file_path.exists() and not self.settings.overwrite_existing_files:
                            self._log.info(
                                "[%s] File exists (no db record) - registering & skip: %s",
                                self.station_name,
                                file_path.name,
                            )
                            try:
                                await self._repo.register(
                                    SavedTrack(
                                        stream_title=clean,
                                        artist=track.artist,
                                        title=track.title,
                                        file_path=str(file_path),
                                        file_size=file_path.stat().st_size,
                                    ),
                                    self.station_name,
                                )
                            except Exception as exc:
                                self._log.warning(
                                    "[%s] failed to register existing file: %s",
                                    self.station_name,
                                    exc,
                                )
                            self._audio_buffer.clear()
                            recording = False
                            continue
                        # ---- max_recordings guard: no new songs at limit ----
                        # Existing songs are still recorded for replace-if-better check.
                        if self.settings.max_recordings is not None:
                            all_records = await self._repo.list_all()
                            if len(
                                all_records
                            ) >= self.settings.max_recordings and not await self._repo.exists(
                                self.station_name, clean
                            ):
                                now = time.monotonic()
                                if now - self._last_limit_log >= 60.0:
                                    self._log.warning(
                                        "[%s] Max recordings (%d) reached — not recording more.",
                                        self.station_name,
                                        self.settings.max_recordings,
                                    )
                                    self._last_limit_log = now
                                self._audio_buffer.clear()
                                recording = False
                                continue
                        try:
                            writer = TrackWriter(
                                file_path,
                                min_size=self.settings.min_file_size_bytes,
                            )
                            # Prepend rolling buffer to recover song-beginning audio
                            # lost to ICY metadata-interval latency
                            if self._audio_buffer:
                                writer.write(bytes(self._audio_buffer))
                                self._audio_buffer.clear()
                            recording = True
                            self._log.info(
                                "[%s] Recording -> %s",
                                self.station_name,
                                file_path.name,
                            )
                        except OSError as exc:
                            self._log.error(
                                "[%s] cannot open %s: %s",
                                self.station_name,
                                file_path,
                                exc,
                            )
                            recording = False
                            writer = None
            # EOF: in-flight incomplete -> discard
            self._log.info("[%s] stream ended (EOF).", self.station_name)
            await _close_writer(finalize=False)
            return True
        except Exception as exc:
            self._log.warning("[%s] stream interrupted: %s", self.station_name, exc)
            await _close_writer(finalize=False)
            return False
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    # ------------------------------------------------------------------ enrichment

    async def _enrich_song(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
    ) -> EnrichedInfo | None:
        return await enrich_song(
            file_path,
            track,
            provenance,
            self.settings,
            self._metadata,
            self._tagger,
            enrich_semaphore=self._enrich_sem,
            file_locks=self._file_locks,
            logger=self._log,
        )

    # ------------------------------------------------------------- fingerprinting

    async def _fingerprint_song(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
    ) -> None:
        await fingerprint_song(
            file_path,
            track,
            self.station_name,
            provenance,
            self.settings,
            self._fingerprint,
            self._repo,
            self._tagger,
            cover_provider=self._cover_provider,
            popularity_provider=self._popularity,
            file_locks=self._file_locks,
            logger=self._log,
        )


def _parse_metaint(headers: dict[str, str]) -> int | None:
    for key in ("icy-metaint", "Icy-Metaint", "ICY-METAINT"):
        val = headers.get(key)
        if val:
            try:
                return int(val)
            except ValueError:
                return None
    return None


__all__ = ["StreamRecorder"]
