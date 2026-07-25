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
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import ConfigurationError
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintProvider,
)
from radio_ripper.services.metadata import (
    CoverArtArchiveProvider,
    ITunesMetadataProvider,
    MetadataProvider,
    NullMetadataProvider,
)
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver, load_local_m3u
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService
from radio_ripper.services.popularity import DeezerPopularityChecker
from radio_ripper.services.repository import SQLiteTrackRepository, TrackRepository
from radio_ripper.services.storage import remove_empty_parents
from radio_ripper.services.stream import StreamRecorder
from radio_ripper.services.tagging import ID3Tagger, TrackTagger
from radio_ripper.services.uploader import Uploader

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
        popularity_provider: DeezerPopularityChecker | None = None,
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
        self.popularity_provider = popularity_provider
        self.resolver = playlist_resolver
        self.logger = logger or _LOGGER
        self._enrich_sem = asyncio.Semaphore(settings.enrichment_workers)
        self._recorders: list[StreamRecorder] = []
        self._cancel_requested = False
        self._uploader: Uploader | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        logger: logging.Logger | None = None,
    ) -> RadioRipperApp:
        """Construct a fully-wired :class:`RadioRipperApp` from settings."""
        log = logger or _LOGGER
        client = HttpxAsyncClient(user_agent=settings.user_agent)
        assert settings.database is not None
        repository = SQLiteTrackRepository(settings.database)
        tagger: TrackTagger = ID3Tagger()
        metadata: MetadataProvider = ITunesMetadataProvider(
            client,
            metadata_timeout=settings.metadata_timeout,
            cover_timeout=settings.cover_timeout,
        )
        resolver = HttpPlaylistResolver(client, timeout=settings.request_timeout)
        with contextlib.suppress(ImportError):
            from dotenv import load_dotenv

            load_dotenv()
        api_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACCOUST_ID", "")
        if not api_key:
            raise ConfigurationError(
                "AcoustID API-Key required. "
                "Set ACOUSTID_API_KEY env var (docker: -e ACOUSTID_API_KEY=your_key) "
                "in a .env file or as environment variable."
            )
        os.environ.setdefault("ACOUSTID_API_URL", "https://api.acoustid.org/v2/lookup")
        fp_provider: FingerprintProvider = AcoustidFingerprintProvider(
            api_key,
            min_score=settings.acoustid_min_score,
        )
        # Cover Art Archive: secondary cover-art source keyed on MusicBrainz
        # recording IDs returned by AcoustID. Used by StreamRecorder when
        # iTunes enrichment returned no artwork.
        cover_provider: Any | None = (
            CoverArtArchiveProvider(client, timeout=settings.cover_timeout)
            if settings.enable_coverartarchive
            else None
        )
        popularity_provider: DeezerPopularityChecker | None = (
            DeezerPopularityChecker(client) if settings.min_popularity_rank > 0 else None
        )
        app = cls(
            settings=settings,
            client=client,
            repository=repository,
            tagger=tagger,
            metadata_provider=metadata,
            fingerprint_provider=fp_provider,
            cover_provider=cover_provider,
            popularity_provider=popularity_provider,
            playlist_resolver=resolver,
            logger=log,
        )
        temp_dir = settings.temp_dir or (settings.work_dir / "temp")
        app._uploader = Uploader(
            inbox=settings.mp3_inbox,
            temp_dir=temp_dir,
            settings=settings,
            fingerprint_provider=fp_provider,
            metadata_provider=metadata,
            repository=repository,
            tagger=tagger,
            name="inbox",
            cover_provider=cover_provider,
            popularity_provider=popularity_provider,
            logger=log,
        )
        return app

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)



    async def _cleanup_orphans(self) -> None:
        """Remove DB records whose ``file_path`` no longer exists on disk.
        Also removes ``.untested.mp3`` files that have no matching DB record.
        """
        all_records = await self.repository.list_all()
        count = 0
        for rec in all_records:
            if not Path(rec.track.file_path).is_file():
                with contextlib.suppress(Exception):
                    await self.repository.remove(rec.station_name, rec.track.stream_title)
                self.logger.info(
                    "Removed orphan DB record (file missing): %s",
                    rec.track.file_path,
                )
                count += 1
        untested_orphans = 0
        if self.settings.destination is not None:
            all_db_paths = {rec.track.file_path for rec in all_records}
            for f in self.settings.destination.rglob("*.untested.mp3"):
                if str(f) not in all_db_paths:
                    with contextlib.suppress(OSError):
                        f.unlink(missing_ok=True)
                        remove_empty_parents(f, self.settings.destination)
                    untested_orphans += 1
        if count:
            self.logger.info("Orphan cleanup: removed %d stale records.", count)
        if untested_orphans:
            self.logger.info(
                "Orphan cleanup: removed %d orphan .untested.mp3 files.", untested_orphans
            )
        if not count and not untested_orphans:
            self.logger.debug("Orphan cleanup: no stale records found.")

    async def _validate_acoustid_key(self) -> None:
        """Check the AcoustID API key with a minimal test request.
        Caches a successful validation in ``work_dir / "acoustid_key.ok"``
        for 24 hours so subsequent restarts skip the API call.
        Raises :class:`ConfigurationError` when the key is rejected.
        """
        cache_file = self.settings.work_dir / "acoustid_key.ok"
        if cache_file.is_file():
            age = time.time() - cache_file.stat().st_mtime
            if age < 86400:
                self.logger.debug("AcoustID key validation cache is fresh (%.0fh).", age / 3600)
                return
            cache_file.unlink(missing_ok=True)

        api_url = os.environ.get(
            "ACOUSTID_API_URL", "https://api.acoustid.org/v2/lookup"
        )
        api_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACCOUST_ID", "")
        params = urllib.parse.urlencode({
            "client": api_key,
            "format": "json",
            "duration": 1,
            "fingerprint": "AQAA",
        })
        url = f"{api_url}?{params}"
        import httpx
        try:
            async with httpx.AsyncClient() as c:
                resp = await c.get(url, timeout=5.0)
                data = resp.json()
                if data.get("status") == "error":
                    err = data.get("error", {}).get("message", "")
                    if "Invalid API key" in err or "invalid key" in err.lower():
                        raise ConfigurationError(
                            f"AcoustID API key rejected: {err}. "
                            "Set a valid ACOUSTID_API_KEY in your .env file."
                        )
                    self.logger.debug(
                        "AcoustID test fingerprint not accepted (key seems valid): %s", err
                    )
                else:
                    self.logger.info("AcoustID API key validated successfully.")
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(str(time.time()), encoding="utf-8")
        except ConfigurationError:
            raise
        except Exception as exc:
            self.logger.warning(
                "AcoustID key validation request failed (non-fatal): %s", exc
            )

    async def start(self) -> None:
        """Create and launch one :class:`StreamRecorder` task per stream."""
        await self._cleanup_orphans()
        if self._cancel_requested:
            self.logger.info("Startup cancelled — not starting streams.")
            return
        if self.fingerprint is not None:
            await self._validate_acoustid_key()
        # Use pre-populated streams when set (API layer or tests); otherwise
        # build from custom.m3u + auto-discovery.
        if not self.settings.streams:
            custom_path = self.settings.work_dir / "stations" / "custom.m3u"
            custom_stations = load_local_m3u(custom_path) if custom_path.is_file() else []
            if custom_stations:
                self.logger.info("Loaded %d stations from custom.m3u.", len(custom_stations))
            else:
                custom_path.parent.mkdir(parents=True, exist_ok=True)
                custom_path.write_text("#EXTM3U\n")
                self.logger.info("Created empty custom.m3u at %s.", custom_path)

            stations = list(custom_stations)

            if not self.settings.disable_automatic_streams:
                discovered = await PlaylistDiscoveryService(self.settings).load_or_discover()
                self.logger.info("Loaded %d stations via discovery.", len(discovered))
                stations.extend(discovered)

            if not stations:
                self.logger.error("No streams available. Exiting.")
                return

            # Cap at max_concurrent_streams — custom stations have priority
            max_streams = self.settings.max_concurrent_streams
            if len(stations) > max_streams:
                custom_count = len(custom_stations)
                if custom_count >= max_streams:
                    stations = stations[:max_streams]
                else:
                    stations = stations[:custom_count] + stations[custom_count:][: max_streams - custom_count]

            self.settings.streams = stations
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
                popularity_provider=self.popularity_provider,
                enrich_semaphore=self._enrich_sem,
                logger=self.logger,
                ad_title_patterns=effective_patterns,
                no_icy_disable_after=self.settings.no_icy_disable_after,
            )
            rec.start()
            self._recorders.append(rec)
        self.logger.info("Started %d stream recorders.", len(self._recorders))
        if self._uploader is not None:
            await self._uploader.start()

    def cancel(self) -> None:
        """Request cancellation of startup reprocessing (thread-safe)."""
        self._cancel_requested = True

    async def stop(self) -> None:
        """Gracefully stop recorders, wait for enrichment tasks, close resources."""
        self._cancel_requested = True
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
        if self._uploader is not None:
            await self._uploader.stop()
        await self.repository.aclose()
        await self.client.aclose()
        self.logger.info("All recorders stopped.")


__all__ = ["RadioRipperApp"]
