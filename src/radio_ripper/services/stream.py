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
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import SavedTrack, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import StreamConnectionError, StreamProtocolError
from radio_ripper.services.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.playlist import PlaylistResolver
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import TrackWriter, compute_file_path
from radio_ripper.services.tagging import TrackTagger

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
        enrich_semaphore: asyncio.Semaphore | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.station_name = station_name
        self.playlist_url = playlist_url
        self.settings = settings
        self._http = http_client
        self._resolver = playlist_resolver
        self._repo = repository
        self._tagger = tagger
        self._metadata = metadata_provider
        self._enrich_sem = enrich_semaphore
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._enrichment_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        self._stop_event.set()

    async def join(self) -> None:
        if self._task is not None:
            await self._task

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(
            self._run_forever(), name=f"Recorder-{self.station_name}"
        )
        return self._task

    # ------------------------------------------------------------------ core loop

    async def _run_forever(self) -> None:
        self._log.info(
            "Starting recorder '%s' for playlist '%s'",
            self.station_name, self.playlist_url,
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
            if ok:
                delay = self.settings.reconnect_base_delay
            else:
                self._log.info(
                    "[%s] Reconnect in %.1fs (max %.1fs)",
                    self.station_name, delay, self.settings.reconnect_max_delay,
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
            return await self._stream_with_meta(stream_url)
        except StreamConnectionError as exc:
            self._log.error("[%s] Request failed: %s", self.station_name, exc)
            return False
        except StreamProtocolError as exc:
            self._log.warning("[%s] Protocol error: %s", self.station_name, exc)
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
            self._log.info("[%s] No icy-metaint header; closing.", self.station_name)
            with contextlib.suppress(Exception):
                await agen.aclose()  # type: ignore[attr-defined]
            return False
        self._log.info("[%s] icy-metaint=%d", self.station_name, metaint)
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
                    current_title = None
                    recording = False
                    writer = None
                    return
                final_path = writer.final_path
                track = TrackInfo.from_stream_title(current_title or "")
                provenance = f"{self.station_name}@{self.playlist_url}"
                try:
                    self._tagger.write_basic(final_path, track, provenance)
                except Exception as exc:
                    self._log.warning("[%s] tag failed: %s", self.station_name, exc)
                saved = SavedTrack(
                    stream_title=track.stream_title,
                    artist=track.artist,
                    title=track.title,
                    file_path=str(final_path),
                    file_size=final_path.stat().st_size,
                )
                try:
                    await self._repo.register(saved, self.station_name)
                except Exception as exc:
                    self._log.warning("[%s] db-register: %s", self.station_name, exc)
                self._log.info(
                    "[%s] Completed: %s (%d bytes)",
                    self.station_name, final_path.name, final_path.stat().st_size,
                )
                # Kick off async enrichment (non-blocking)
                if self._metadata and self.settings.enrich_metadata:
                    enrich_task = asyncio.create_task(
                        self._enrich_song(final_path, track, provenance)
                    )
                    self._enrichment_tasks.add(enrich_task)
                    enrich_task.add_done_callback(self._enrichment_tasks.discard)
            else:
                writer.discard()
                self._log.info(
                    "[%s] Discarded incomplete: %s (%d bytes)",
                    self.station_name, writer.final_path.name, writer.size,
                )
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
                        if recording and writer is not None:
                            writer.write(event.data)
                        # else: phase 1 / duplicate mode -> discard bytes
                    elif isinstance(event, TitleChanged):
                        new_title = event.title
                        if first_title_seen is None:
                            first_title_seen = new_title
                            current_title = new_title
                            self._log.info(
                                "[%s] Joined mid-song '%s' - waiting for next boundary.",
                                self.station_name, new_title,
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
                            recording = False
                            continue
                        track = TrackInfo.from_stream_title(clean)
                        if await self._repo.exists(self.station_name, clean):
                            self._log.info(
                                "[%s] Skipping duplicate: %s",
                                self.station_name, clean,
                            )
                            recording = False
                            continue
                        file_path = compute_file_path(
                            self.settings.destination,
                            self.station_name,
                            track.artist,
                            track.title,
                            clean,
                            overwrite=self.settings.overwrite_existing_files,
                        )
                        if file_path.exists() and not self.settings.overwrite_existing_files:
                            self._log.info(
                                "[%s] File exists (no db record) - registering & skip: %s",
                                self.station_name, file_path.name,
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
                                    self.station_name, exc,
                                )
                            recording = False
                            continue
                        try:
                            writer = TrackWriter(
                                file_path,
                                min_size=self.settings.min_file_size_bytes,
                            )
                            recording = True
                            self._log.info(
                                "[%s] Recording -> %s",
                                self.station_name, file_path.name,
                            )
                        except OSError as exc:
                            self._log.error(
                                "[%s] cannot open %s: %s",
                                self.station_name, file_path, exc,
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
                await agen.aclose()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ enrichment

    async def _enrich_song(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
    ) -> None:
        if self._metadata is None:
            return
        sem = self._enrich_sem
        try:
            if sem is not None:
                await sem.acquire()
            await self._enrich_song_inner(file_path, track, provenance)
        except Exception:
            self._log.exception(
                "[%s] enrichment failed for %s", self.station_name, file_path.name
            )
        finally:
            if sem is not None:
                sem.release()

    async def _enrich_song_inner(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
    ) -> None:
        assert self._metadata is not None
        info = await self._metadata.fetch(track.artist, track.title)
        if info is None:
            self._log.info(
                "[%s] no enrichment hit for: %s - %s",
                self.station_name, track.artist, track.title,
            )
            try:
                await self._repo.update_enrichment(
                    self.station_name, track.stream_title,
                    artist=track.artist, title=track.title,
                    enrichment="miss",
                )
            except Exception as exc:
                self._log.debug("[%s] db enrichment-miss update: %s", self.station_name, exc)
            return
        cover: bytes | None = None
        if self.settings.embed_cover_art and info.artwork_url:
            cover = await self._metadata.download_image(info.artwork_url)
        try:
            self._tagger.write_full(file_path, track, info, cover, provenance)
        except Exception as exc:
            self._log.warning(
                "[%s] tag-enrichment failed %s: %s",
                self.station_name, file_path.name, exc,
            )
        self._log.info(
            "[%s] Enriched: %s | album=%s year=%s cover=%s",
            self.station_name, file_path.name,
            info.album or "-", info.year or "-",
            "yes" if cover else "no",
        )
        try:
            await self._repo.update_enrichment(
                self.station_name, track.stream_title,
                artist=info.artist or track.artist,
                title=info.title or track.title,
                album=info.album,
                year=info.year,
                file_size=file_path.stat().st_size if file_path.exists() else None,
                has_cover=cover is not None,
                enrichment="itunes",
            )
        except Exception as exc:
            self._log.debug("[%s] db enrichment update: %s", self.station_name, exc)


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
