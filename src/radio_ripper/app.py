"""Application orchestrator — wires up all services for the radio ripper.

A :class:`RadioRipperApp` is constructed from a validated :class:`Settings`
instance. It owns the shared :class:`~radio_ripper.infra.http.AsyncHttpClient`,
a :class:`~radio_ripper.services.repository.TrackRepository`, a
:class:`~radio_ripper.services.tagging.TrackTagger`, and a
:class:`~radio_ripper.services.metadata.MetadataProvider`; and spawns one
:class:`~radio_ripper.services.stream.StreamRecorder` task per station.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintError,
    FingerprintProvider,
    NullFingerprintProvider,
)
from radio_ripper.services.metadata import (
    CoverArtArchiveProvider,
    ITunesMetadataProvider,
    MetadataProvider,
    NullMetadataProvider,
)
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService
from radio_ripper.services.repository import SQLiteTrackRepository, TrackRepository
from radio_ripper.services.storage import (
    compute_file_path,
    remove_empty_parents,
)
from radio_ripper.services.stream import StreamRecorder
from radio_ripper.services.tagging import ID3Tagger, TrackTagger

if TYPE_CHECKING:
    from collections.abc import Sequence

_LOGGER = logging.getLogger("radio_ripper.app")


class RadioRipperApp:
    """Compose services and run all stream recorders concurrently."""

    def __init__(
        self,
        *,
        settings: Settings,
        client: AsyncHttpClient,
        repository: TrackRepository,
        tagger: TrackTagger,
        metadata_provider: MetadataProvider,
        fingerprint_provider: FingerprintProvider | None = None,
        cover_provider: Any | None = None,
        playlist_resolver: PlaylistResolver,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self.tagger = tagger
        self.metadata = metadata_provider
        self.fingerprint = fingerprint_provider
        self.cover_provider = cover_provider
        self.resolver = playlist_resolver
        self.logger = logger or _LOGGER
        self._enrich_sem = asyncio.Semaphore(settings.enrichment_workers)
        self._recorders: list[StreamRecorder] = []

    @classmethod
    def from_settings(
        cls, settings: Settings, *, logger: logging.Logger | None = None
    ) -> RadioRipperApp:
        """Construct a fully-wired :class:`RadioRipperApp` from settings."""
        log = logger or _LOGGER
        client = HttpxAsyncClient(user_agent=settings.user_agent)
        repository = SQLiteTrackRepository(settings.database)
        tagger: TrackTagger = ID3Tagger()
        metadata: MetadataProvider = (
            ITunesMetadataProvider(
                client,
                metadata_timeout=settings.metadata_timeout,
                cover_timeout=settings.cover_timeout,
            )
            if settings.enrich_metadata
            else NullMetadataProvider()
        )
        resolver = HttpPlaylistResolver(client, timeout=settings.request_timeout)
        with contextlib.suppress(ImportError):
            from dotenv import load_dotenv

            load_dotenv()
        api_key = settings.acoustid_api_key or os.environ.get("ACCOUST_ID", "")
        fingerprint: FingerprintProvider = (
            AcoustidFingerprintProvider(
                api_key,
                min_score=settings.acoustid_min_score,
            )
            if api_key
            else NullFingerprintProvider()
        )
        # Cover Art Archive: secondary cover-art source keyed on MusicBrainz
        # recording IDs returned by AcoustID. Used by StreamRecorder when
        # iTunes enrichment returned no artwork.
        cover_provider: Any | None = (
            CoverArtArchiveProvider(client, timeout=settings.cover_timeout)
            if settings.enable_coverartarchive
            else None
        )
        return cls(
            settings=settings,
            client=client,
            repository=repository,
            tagger=tagger,
            metadata_provider=metadata,
            fingerprint_provider=fingerprint,
            cover_provider=cover_provider,
            playlist_resolver=resolver,
            logger=log,
        )

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)

    async def _reprocess_all(self) -> None:
        """Restructure all existing ``.mp3`` files and reset to ``.untested.mp3``.

        Triggered by ``settings.reprocess_all``. For each file:
        1. Looks up the DB record.
        2. If enrichment data is missing, fetches it from iTunes.
        3. Computes the new path without the station fallback folder
           (``{Artist}[/{Album}]/{Song}.mp3``).
        4. Moves the file and removes empty old directories.
        5. Renames ``.mp3`` to ``.untested.mp3`` for a fresh fingerprint pass.

        Runs before :meth:`reprocess_untested`.
        """
        if not self.settings.reprocess_all:
            return
        self.logger.info("Reprocess-all enabled — restructuring + resetting to .untested…")
        count = 0
        for mp3 in sorted(self.settings.destination.rglob("*.mp3")):
            if mp3.suffix != ".mp3" or mp3.name.endswith(".untested.mp3"):
                continue
            record = await self.repository.find_by_file_path(str(mp3))
            if record is None:
                self.logger.warning("No DB entry for %s — skipping", mp3)
                continue

            # Enrichment: use stored album or fetch from iTunes
            album = record.track.album
            if not album and not isinstance(self.metadata, NullMetadataProvider):
                async with self._enrich_sem:
                    try:
                        info = await self.metadata.fetch(
                            record.track.artist, record.track.title
                        )
                        album = info.album if info else None
                    except Exception as exc:
                        self.logger.debug(
                            "[%s] enrichment fetch failed: %s", record.station_name, exc
                        )

            # Compute new path without station fallback
            new_path = compute_file_path(
                self.settings.destination,
                record.track.artist,
                record.track.title,
                record.track.stream_title,
                album=album,
                overwrite=True,
            )
            new_untested = new_path.with_name(new_path.stem + ".untested.mp3")

            # Move + rename to .untested (or just rename if path unchanged)
            if mp3 != new_untested:
                new_untested.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(mp3), str(new_untested))
                    remove_empty_parents(mp3, self.settings.destination)
                except OSError as exc:
                    self.logger.warning("Move %s -> %s failed: %s", mp3, new_untested, exc)
                    continue
            else:
                try:
                    mp3.rename(new_untested)
                except OSError as exc:
                    self.logger.warning(
                        "Rename %s -> %s failed: %s", mp3, new_untested.name, exc
                    )
                    continue

            # Persist album to DB if enrichment succeeded
            if album:
                try:
                    await self.repository.update_enrichment(
                        record.station_name,
                        record.track.stream_title,
                        album=album,
                        enrichment="itunes",
                    )
                except Exception as exc:
                    self.logger.debug("[%s] db enrichment update: %s", record.station_name, exc)
            await self.repository.update_file_path(
                record.station_name, record.track.stream_title, str(new_untested)
            )
            count += 1
        self.logger.info("Reprocess-all: %d files restructured + reset to .untested.", count)

    async def reprocess_untested(self) -> None:
        """Re-fingerprint ``.untested.mp3`` files left from a previous run."""
        if self.fingerprint is None or isinstance(self.fingerprint, NullFingerprintProvider):
            self.logger.debug("No AcoustID provider — skipping untested reprocess.")
            return
        records = await self.repository.list_untested()
        if not records:
            return
        self.logger.info("Re-fingerprinting %d untested files from previous run…", len(records))
        min_interval = self.settings.acoustid_min_interval_s
        last_fp_call = 0.0
        for rec in records:
            p = Path(rec.track.file_path)
            if not p.is_file():
                self.logger.warning("Untested file missing: %s", p)
                continue
            if min_interval > 0:
                now = time.monotonic()
                wait = min_interval - (now - last_fp_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                last_fp_call = time.monotonic()
            try:
                result = await self.fingerprint.fingerprint(p)
            except FingerprintError as exc:
                self.logger.warning(
                    "Fingerprint infrastructure error for %s: %s "
                    "(file kept as .untested.mp3 for next retry)",
                    p.name,
                    exc,
                    exc_info=True,
                )
                continue
            except Exception:
                self.logger.debug(
                    "unexpected fingerprint error for %s",
                    p.name,
                    exc_info=True,
                )
                continue
            if result is None:
                self.logger.info("Still no AcoustID match for %s", p.name)
                if self.settings.discard_unmatched:
                    with contextlib.suppress(OSError):
                        p.unlink(missing_ok=True)
                        remove_empty_parents(p, self.settings.destination)
                    try:
                        await self.repository.remove(rec.station_name, rec.track.stream_title)
                    except Exception as exc:
                        self.logger.debug("db remove after no-match: %s", exc)
                    self.logger.info("Discarded (still no match): %s", p.name)
                continue
            new_path = p.with_name(p.stem.replace(".untested", "") + ".mp3")
            if new_path.exists():
                # Don't silently clobber an existing .mp3 — keep .untested.mp3
                self.logger.warning(
                    "Refuse to rename %s -> %s (target exists). "
                    "Keeping .untested.mp3 for manual review.",
                    p.name,
                    new_path.name,
                )
                continue
            try:
                p.rename(new_path)
            except OSError as exc:
                self.logger.warning("rename %s -> %s failed: %s", p.name, new_path.name, exc)
                continue
            try:
                self.tagger.update_acoustid(new_path, result.recording_id, result.score)
            except Exception as exc:
                self.logger.debug("acoustid tag update: %s", exc)
            try:
                await self.repository.update_file_path(
                    rec.station_name, rec.track.stream_title, str(new_path)
                )
            except Exception as exc:
                self.logger.debug("db update_file_path: %s", exc)
            try:
                await self.repository.update_fingerprint(
                    rec.station_name,
                    rec.track.stream_title,
                    recording_id=result.recording_id,
                    score=result.score,
                )
            except Exception as exc:
                self.logger.debug("db update_fingerprint: %s", exc)
            self.logger.info("Re-fingerprinted OK: %s", new_path.name)
        self.logger.info("Untested reprocess complete (%d files).", len(records))

    async def start(self) -> None:
        """Create and launch one :class:`StreamRecorder` task per stream."""
        await self._reprocess_all()
        await self.reprocess_untested()
        if not self.settings.streams:
            discovered = await PlaylistDiscoveryService(self.settings).load_or_discover()
            if not discovered:
                self.logger.error("No streams discovered and none configured. Exiting.")
                return
            self.settings.streams = discovered
            self.logger.info("Loaded %d stations via discovery.", len(discovered))
        for stream in self.settings.streams:
            if not stream.enabled:
                self.logger.info("Skipping disabled stream: %s", stream.name)
                continue
            effective_patterns = (
                stream.ad_title_patterns
                if stream.ad_title_patterns is not None
                else self.settings.ad_title_patterns
            )
            rec = StreamRecorder(
                station_name=stream.name,
                playlist_url=str(stream.url),
                settings=self.settings,
                http_client=self.client,
                playlist_resolver=self.resolver,
                repository=self.repository,
                tagger=self.tagger,
                metadata_provider=self.metadata,
                fingerprint_provider=self.fingerprint,
                cover_provider=self.cover_provider,
                enrich_semaphore=self._enrich_sem,
                logger=self.logger,
                ad_title_patterns=effective_patterns,
                no_icy_disable_after=self.settings.no_icy_disable_after,
            )
            rec.start()
            self._recorders.append(rec)
        self.logger.info("Started %d stream recorders.", len(self._recorders))

    async def stop(self) -> None:
        """Gracefully stop recorders, wait for enrichment tasks, close resources."""
        self.logger.info("Stopping all recorders...")
        for rec in self._recorders:
            rec.stop()
        for rec in self._recorders:
            try:
                await asyncio.wait_for(rec.join(), timeout=10.0)
            except TimeoutError:
                self.logger.warning("Recorder %s did not stop in time.", rec.station_name)
        # Drain enrichment tasks; they're short-lived.
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and "enrich" in t.get_name()
        ]
        for task in pending:
            try:
                await asyncio.wait_for(task, timeout=15.0)
            except TimeoutError:
                task.cancel()
        await self.repository.aclose()
        await self.client.aclose()
        self.logger.info("All recorders stopped.")


__all__ = ["RadioRipperApp"]
