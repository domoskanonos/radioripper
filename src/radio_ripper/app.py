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
import logging
from typing import TYPE_CHECKING

from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.metadata import (
    ITunesMetadataProvider,
    MetadataProvider,
    NullMetadataProvider,
)
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver
from radio_ripper.services.repository import SQLiteTrackRepository, TrackRepository
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
        playlist_resolver: PlaylistResolver,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self.tagger = tagger
        self.metadata = metadata_provider
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
        return cls(
            settings=settings,
            client=client,
            repository=repository,
            tagger=tagger,
            metadata_provider=metadata,
            playlist_resolver=resolver,
            logger=log,
        )

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)

    async def start(self) -> None:
        """Create and launch one :class:`StreamRecorder` task per stream."""
        if not self.settings.streams:
            self.logger.error("No streams configured. Exiting.")
            return
        for stream in self.settings.streams:
            effective_patterns = (
                stream.ad_title_patterns
                if stream.ad_title_patterns is not None
                else self.settings.ad_title_patterns
            )
            effective_pre_buffer = (
                stream.pre_buffer_bytes
                if stream.pre_buffer_bytes is not None
                else self.settings.pre_buffer_bytes
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
                enrich_semaphore=self._enrich_sem,
                logger=self.logger,
                ad_title_patterns=effective_patterns,
                pre_buffer_bytes=effective_pre_buffer,
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
