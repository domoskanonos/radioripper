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
from typing import TYPE_CHECKING

from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintProvider,
    NullFingerprintProvider,
)
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService
from radio_ripper.services.metadata import (
    ITunesMetadataProvider,
    MetadataProvider,
    NullMetadataProvider,
)
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver
from radio_ripper.services.repository import SQLiteTrackRepository, TrackRepository
from radio_ripper.services.storage import remove_empty_parents
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
        playlist_resolver: PlaylistResolver,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self.tagger = tagger
        self.metadata = metadata_provider
        self.fingerprint = fingerprint_provider
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
        return cls(
            settings=settings,
            client=client,
            repository=repository,
            tagger=tagger,
            metadata_provider=metadata,
            fingerprint_provider=fingerprint,
            playlist_resolver=resolver,
            logger=log,
        )

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)

    async def reprocess_untested(self) -> None:
        """Re-fingerprint ``.untested.mp3`` files left from a previous run."""
        if not isinstance(self.fingerprint, AcoustidFingerprintProvider):
            self.logger.debug("No AcoustID provider — skipping untested reprocess.")
            return
        records = await self.repository.list_untested()
        if not records:
            return
        self.logger.info("Re-fingerprinting %d untested files from previous run…", len(records))
        for rec in records:
            p = Path(rec.track.file_path)
            if not p.is_file():
                self.logger.warning("Untested file missing: %s", p)
                continue
            try:
                result = await self.fingerprint.fingerprint(p)
            except Exception:
                self.logger.debug("fingerprint error for %s", p.name)
                continue
            if result is None:
                self.logger.info("Still no AcoustID match for %s", p.name)
                if self.settings.discard_unmatched:
                    with contextlib.suppress(OSError):
                        p.unlink(missing_ok=True)
                        remove_empty_parents(p, self.settings.destination)
                    await self.repository.remove(rec.station_name, rec.track.stream_title)
                    self.logger.info("Discarded (still no match): %s", p.name)
                continue
            new_path = p.with_name(p.stem.replace(".untested", "") + ".mp3")
            with contextlib.suppress(OSError):
                p.rename(new_path)
            try:
                self.tagger.update_acoustid(new_path, result.recording_id, result.score)
            except Exception as exc:
                self.logger.debug("acoustid tag update: %s", exc)
            await self.repository.update_file_path(
                rec.station_name, rec.track.stream_title, str(new_path)
            )
            await self.repository.update_fingerprint(
                rec.station_name, rec.track.stream_title,
                recording_id=result.recording_id, score=result.score,
            )
            self.logger.info("Re-fingerprinted OK: %s", new_path.name)
        self.logger.info("Untested reprocess complete (%d files).", len(records))

    async def start(self) -> None:
        """Create and launch one :class:`StreamRecorder` task per stream."""
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
