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
import shutil
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import EnrichedInfo, SavedTrack, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import StreamConnectionError, StreamProtocolError
from radio_ripper.services.fingerprint import FingerprintError, FingerprintProvider
from radio_ripper.services.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.playlist import PlaylistResolver
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import (
    TrackWriter,
    compute_file_path,
    enforce_recording_limit,
    get_mp3_duration,
    remove_empty_parents,
    remux_mp3,
    sanitize_filename,
)
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
        fingerprint_provider: FingerprintProvider | None = None,
        cover_provider: Any | None = None,
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
                # Fix MP3 frame alignment caused by ICY stream cut-points
                # (must run BEFORE tagging so tags aren't stripped by pydub/ffmpeg)
                remux_mp3(final_path)
                # Duration check: discard songs shorter than the configured minimum
                min_dur = self.settings.min_duration_s
                if min_dur > 0:
                    dur = get_mp3_duration(final_path)
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
                        current_title = None
                        recording = False
                        writer = None
                        return
                track = TrackInfo.from_stream_title(current_title or "")
                provenance = f"{self.station_name}@{self.playlist_url}"
                try:
                    self._tagger.write_basic(final_path, track, provenance)
                except Exception as exc:
                    self._log.warning("[%s] tag failed: %s", self.station_name, exc)
                # Synchronous enrichment: fetch metadata, write ID3 tags
                info: EnrichedInfo | None = None
                if self._metadata and self.settings.enrich_metadata:
                    info = await self._enrich_song(final_path, track, provenance)

                # Move file into album subfolder if enrichment found an album
                if info and info.album:
                    artist_dir = sanitize_filename(info.artist or track.artist)
                    album_dir = sanitize_filename(info.album)
                    new_dir = self.settings.destination / artist_dir / album_dir
                    new_dir.mkdir(parents=True, exist_ok=True)
                    new_path = new_dir / final_path.name
                    shutil.move(str(final_path), str(new_path))
                    remove_empty_parents(final_path, self.settings.destination)
                    final_path = new_path

                saved = SavedTrack(
                    stream_title=track.stream_title,
                    artist=info.artist if info else track.artist,
                    title=info.title if info else track.title,
                    file_path=str(final_path),
                    file_size=final_path.stat().st_size,
                    album=info.album if info else None,
                    year=info.year if info else None,
                    has_cover=(info is not None),
                    enrichment="itunes" if info else None,
                )
                try:
                    await self._repo.register(saved, self.station_name)
                except Exception as exc:
                    self._log.warning("[%s] db-register: %s", self.station_name, exc)
                self._log.info(
                    "[%s] Completed: %s (%d bytes)",
                    self.station_name,
                    final_path.name,
                    final_path.stat().st_size,
                )
                # Enforce global recording limit
                max_rec = self.settings.max_recordings
                if max_rec is not None:
                    deleted = enforce_recording_limit(self.settings.destination, max_rec)
                    for d in deleted:
                        self._log.info(
                            "[%s] Limit %d reached - deleted oldest: %s",
                            self.station_name,
                            max_rec,
                            d.name,
                        )
                # Kick off async fingerprinting (non-blocking)
                if self._fingerprint is not None:
                    fp_task = asyncio.create_task(
                        self._fingerprint_song(final_path, track, provenance)
                    )
                    self._enrichment_tasks.add(fp_task)
                    fp_task.add_done_callback(self._enrichment_tasks.discard)
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
                            recording = False
                            continue
                        if self._is_ad_title(clean):
                            self._log.info(
                                "[%s] Ad title detected, skipping: %s",
                                self.station_name,
                                clean,
                            )
                            recording = False
                            continue
                        try:
                            if await self._repo.exists(self.station_name, clean):
                                self._log.info(
                                    "[%s] Skipping duplicate (already in DB): %s",
                                    self.station_name,
                                    clean,
                                )
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
        if self._metadata is None:
            return None
        sem = self._enrich_sem
        try:
            if sem is not None:
                await sem.acquire()
            async with self._lock_for(file_path):
                return await self._enrich_song_inner(file_path, track, provenance)
        except Exception:
            self._log.exception("[%s] enrichment failed for %s", self.station_name, file_path.name)
            return None
        finally:
            if sem is not None:
                sem.release()

    async def _enrich_song_inner(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
    ) -> EnrichedInfo | None:
        assert self._metadata is not None
        info = await self._metadata.fetch(track.artist, track.title)

        # Load station fallback cover (e.g. station logo) from config
        fallback_cover: bytes | None = None
        if self.settings.fallback_cover_path is not None:
            with contextlib.suppress(OSError):
                fallback_cover = self.settings.fallback_cover_path.read_bytes()

        if info is None:
            self._log.info(
                "[%s] no enrichment hit for: %s - %s",
                self.station_name,
                track.artist,
                track.title,
            )
            # Embed fallback cover even when no enrichment data is found
            if fallback_cover and self.settings.embed_cover_art:
                try:
                    self._tagger.write_full(
                        file_path,
                        track,
                        EnrichedInfo(),
                        None,
                        provenance,
                        fallback_cover=fallback_cover,
                    )
                except Exception as exc:
                    self._log.warning(
                        "[%s] fallback-cover embed failed %s: %s",
                        self.station_name,
                        file_path.name,
                        exc,
                    )
            return None
        cover: bytes | None = None
        if self.settings.embed_cover_art and info.artwork_url:
            cover = await self._metadata.download_image(info.artwork_url)
        try:
            self._tagger.write_full(
                file_path,
                track,
                info,
                cover,
                provenance,
                fallback_cover=fallback_cover,
            )
        except Exception as exc:
            self._log.warning(
                "[%s] tag-enrichment failed %s: %s",
                self.station_name,
                file_path.name,
                exc,
            )
        self._log.info(
            "[%s] Enriched: %s | album=%s year=%s cover=%s",
            self.station_name,
            file_path.name,
            info.album or "-",
            info.year or "-",
            "yes" if (cover or fallback_cover) else "no",
        )
        return info

    # ------------------------------------------------------------- fingerprinting

    async def _fingerprint_song(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
    ) -> None:
        if self._fingerprint is None:
            return
        # Hold the per-file lock for the entire body so enrichment (which may
        # write ID3 tags to file_path) can't race with rename / unlink here.
        # _fingerprint_song is the terminal operation on a recording, so we
        # also pop the lock on exit.
        lock = self._lock_for(file_path)
        try:
            async with lock:
                try:
                    result = await self._fingerprint.fingerprint(file_path)
                except FingerprintError as exc:
                    self._log.warning(
                        "[%s] fingerprint infrastructure error for %s: %s "
                        "(file kept as .untested.mp3 for retry)",
                        self.station_name,
                        file_path.name,
                        exc,
                        exc_info=True,
                    )
                    return
                except Exception:
                    self._log.debug(
                        "[%s] unexpected fingerprint error for %s",
                        self.station_name,
                        file_path.name,
                    )
                    return
                if result is None:
                    self._log.info(
                        "[%s] No AcoustID match: %s",
                        self.station_name,
                        file_path.name,
                    )
                    # Fallback dedup: check if the same artist+title already exists
                    # and has an AcoustID match.  A matched recording is always
                    # preferable to an unmatched one.
                    if track.artist and track.title:
                        try:
                            all_artist_title = await self._repo.find_all_by_artist_title(
                                track.artist,
                                track.title,
                            )
                        except Exception:
                            all_artist_title = []
                        # If ANY existing recording already has an AcoustID match,
                        # discard this new, unmatched copy.
                        has_matched = any(
                            e.track.acoustid_recording_id
                            for e in all_artist_title
                            if not (
                                e.station_name == self.station_name
                                and e.track.stream_title.lower() == track.stream_title.lower()
                            )
                        )
                        if has_matched:
                            self._log.info(
                                "[%s] AcoustID unmatched, but a matched version"
                                " already exists — discarding new: %s",
                                self.station_name,
                                file_path.name,
                            )
                            with contextlib.suppress(OSError):
                                file_path.unlink(missing_ok=True)
                                remove_empty_parents(file_path, self.settings.destination)
                            try:
                                await self._repo.remove(self.station_name, track.stream_title)
                            except Exception as exc:
                                self._log.debug(
                                    "[%s] db remove after fallback-dup: %s",
                                    self.station_name,
                                    exc,
                                )
                            return
                    if self.settings.discard_unmatched:
                        with contextlib.suppress(OSError):
                            file_path.unlink(missing_ok=True)
                            remove_empty_parents(file_path, self.settings.destination)
                        try:
                            await self._repo.remove(self.station_name, track.stream_title)
                        except Exception as exc:
                            self._log.debug(
                                "[%s] db remove after no-match: %s", self.station_name, exc
                            )
                        self._log.info(
                            "[%s] Discarded (no AcoustID match): %s",
                            self.station_name,
                            file_path.name,
                        )
                    return

                self._log.info(
                    "[%s] AcoustID match (score=%.2f): %s - %s (rec=%s)",
                    self.station_name,
                    result.score,
                    result.artist,
                    result.title,
                    result.recording_id,
                )

                # Rename .untested.mp3 → .mp3
                new_path = file_path.with_name(file_path.stem.replace(".untested", "") + ".mp3")
                if new_path.exists():
                    self._log.warning(
                        "[%s] Refuse to rename %s -> %s (target exists). "
                        "Keeping .untested.mp3 for manual review.",
                        self.station_name,
                        file_path.name,
                        new_path.name,
                    )
                    return
                try:
                    file_path.rename(new_path)
                except OSError as exc:
                    self._log.warning(
                        "[%s] rename %s -> %s failed: %s",
                        self.station_name,
                        file_path.name,
                        new_path.name,
                        exc,
                    )
                    return
                # Write AcoustID info into ID3 tags on the new path
                try:
                    self._tagger.update_acoustid(new_path, result.recording_id, result.score)
                except Exception as exc:
                    self._log.debug("[%s] acoustid tag update: %s", self.station_name, exc)
                # Update DB file_path to the new name
                try:
                    await self._repo.update_file_path(
                        self.station_name, track.stream_title, str(new_path)
                    )
                except Exception as exc:
                    self._log.debug("[%s] db update_file_path: %s", self.station_name, exc)

                # Persist fingerprint result in the DB
                try:
                    await self._repo.update_fingerprint(
                        self.station_name,
                        track.stream_title,
                        recording_id=result.recording_id,
                        score=result.score,
                    )
                except Exception as exc:
                    self._log.debug("[%s] db update_fingerprint: %s", self.station_name, exc)

                # Try to fetch cover art from Cover Art Archive using the
                # MusicBrainz recording_id. Best effort — if it fails or no
                # cover is available, the file stays as-is.
                if result.recording_id and self._cover_provider is not None:
                    try:
                        cover_bytes = await self._cover_provider.fetch_cover_by_recording_id(
                            result.recording_id
                        )
                    except Exception as exc:
                        self._log.debug(
                            "[%s] Cover Art Archive lookup failed: %s",
                            self.station_name,
                            exc,
                        )
                        cover_bytes = None
                    if cover_bytes is not None:
                        try:
                            self._tagger.embed_cover(new_path, cover_bytes)
                            self._log.info(
                                "[%s] Embedded CAA cover: %s",
                                self.station_name,
                                new_path.name,
                            )
                        except Exception as exc:
                            self._log.debug(
                                "[%s] embed CAA cover failed: %s",
                                self.station_name,
                                exc,
                            )

                if not result.recording_id:
                    return

                # Cross-station dedup: among ALL existing records with the same
                # recording_id plus the new recording, keep only the one with the
                # highest AcoustID score.  This guarantees there is never more than
                # one copy of the same identified recording on disk.
                try:
                    all_existing = await self._repo.find_all_by_recording_id(result.recording_id)
                except Exception as exc:
                    self._log.debug("[%s] find_all_by_recording_id: %s", self.station_name, exc)
                    return

                if all_existing:
                    candidates: list[tuple[float, str, str, Path]] = [
                        (
                            e.track.acoustid_score or 0.0,
                            e.station_name,
                            e.track.stream_title,
                            Path(e.track.file_path),
                        )
                        for e in all_existing
                    ]
                    # Add the current (new) recording as a candidate.
                    # new_path is the already-renamed file.
                    candidates.append(
                        (
                            result.score,
                            self.station_name,
                            track.stream_title,
                            new_path,
                        )
                    )
                    # Sort descending by score so the best is first.
                    candidates.sort(key=lambda c: c[0], reverse=True)

                    # The best score wins — keep that one, delete all others.
                    (best_score, best_station, best_stream, best_path) = candidates[0]
                    for score, station, stream_title, p in candidates:
                        if (score, station, stream_title, p) == (
                            best_score,
                            best_station,
                            best_stream,
                            best_path,
                        ):
                            continue
                        self._log.info(
                            "[%s] AcoustID dedup: discarding inferior (score %.2f < best %.2f): %s",
                            self.station_name,
                            score,
                            best_score,
                            p.name,
                        )
                        with contextlib.suppress(OSError):
                            p.unlink(missing_ok=True)
                            remove_empty_parents(p, self.settings.destination)
                        try:
                            await self._repo.remove(station, stream_title)
                        except Exception as exc:
                            self._log.debug(
                                "[%s] db remove dedup: %s",
                                self.station_name,
                                exc,
                            )

                # After recording_id dedup: remove any existing recording with the
                # same artist+title that has NO AcoustID match.  A matched version
                # (the current one) is always preferable to an unmatched one.
                if track.artist and track.title:
                    try:
                        unmatched = await self._repo.find_all_by_artist_title(
                            track.artist,
                            track.title,
                        )
                    except Exception:
                        unmatched = []
                    for rec in unmatched:
                        # Skip the current recording itself
                        if (
                            rec.station_name == self.station_name
                            and rec.track.stream_title.lower() == track.stream_title.lower()
                        ):
                            continue
                        # If it already has a recording_id, it's handled by the
                        # dedup above (or it's a different version — keep it).
                        if rec.track.acoustid_recording_id:
                            continue
                        self._log.info(
                            "[%s] Replacing unmatched recording with matched version: %s",
                            self.station_name,
                            rec.track.file_path,
                        )
                        old_path = Path(rec.track.file_path)
                        with contextlib.suppress(OSError):
                            old_path.unlink(missing_ok=True)
                            remove_empty_parents(old_path, self.settings.destination)
                        try:
                            await self._repo.remove(rec.station_name, rec.track.stream_title)
                        except Exception as exc:
                            self._log.debug(
                                "[%s] db remove unmatched for replacement: %s",
                                self.station_name,
                                exc,
                            )
        finally:
            self._release_lock(file_path)


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
