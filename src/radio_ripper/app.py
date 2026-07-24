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

from radio_ripper.domain.models import TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.infra.errors import ConfigurationError
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata import (
    CoverArtArchiveProvider,
    ITunesMetadataProvider,
    MetadataProvider,
    NullMetadataProvider,
)
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService
from radio_ripper.services.popularity import DeezerPopularityChecker
from radio_ripper.services.repository import SQLiteTrackRepository, TrackRepository
from radio_ripper.services.storage import (
    compute_file_path,
    remove_empty_parents,
)
from radio_ripper.services.stream import StreamRecorder, apply_fingerprint_match
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

    @classmethod
    def from_settings(
        cls, settings: Settings, *, logger: logging.Logger | None = None
    ) -> RadioRipperApp:
        """Construct a fully-wired :class:`RadioRipperApp` from settings."""
        log = logger or _LOGGER
        client = HttpxAsyncClient(user_agent=settings.user_agent)
        assert settings.database is not None
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
        api_key = os.environ.get("ACCOUST_ID", "") or settings.acoustid_api_key
        if not api_key:
            raise ConfigurationError(
                "AcoustID API-Key required. "
                "Set ACCOUST_ID env var (docker: -e ACCOUST_ID=your_key) "
                "or acoustid_api_key in config.json."
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
            DeezerPopularityChecker(client)
            if settings.min_popularity_rank > 0
            else None
        )
        return cls(
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

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)

    async def _reprocess_all(self) -> None:
        """Restructure existing ``.mp3`` files to the new folder layout.

        Triggered by ``settings.reprocess_all``. For each file:
        1. Looks up the DB record.
        2. If enrichment data is missing, fetches it from iTunes.
        3. Computes the new path without the station fallback folder
           (``{Artist}[/{Album}]/{Song}.mp3``).
        4. Moves the file, removes empty old directories, updates the DB.
        5. Tries AcoustID fingerprint (if not yet matched) and fetches
           CAA cover art — identical to the live recording flow.
        """
        if not self.settings.reprocess_all:
            return
        self.logger.info("Reprocess-all enabled — restructuring files…")
        count = 0
        min_interval = self.settings.acoustid_min_interval_s
        last_fp_call = 0.0
        for mp3 in sorted(self.settings.destination.rglob("*.mp3")):
            if mp3.suffix != ".mp3" or mp3.name.endswith(".untested.mp3"):
                continue
            record = await self.repository.find_by_file_path(str(mp3))
            if record is None:
                self.logger.warning("No DB entry for %s — skipping", mp3)
                continue

            # Fetch enrichment (always when provider is available)
            info = None
            if not isinstance(self.metadata, NullMetadataProvider):
                async with self._enrich_sem:
                    try:
                        info = await self.metadata.fetch(record.track.artist, record.track.title)
                    except Exception as exc:
                        self.logger.debug(
                            "[%s] enrichment fetch failed: %s", record.station_name, exc
                        )

            album = info.album if info else record.track.album

            new_path = compute_file_path(
                self.settings.destination,
                record.track.artist,
                record.track.title,
                record.track.stream_title,
                album=album,
                overwrite=True,
            )

            if mp3 != new_path:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(mp3), str(new_path))
                    remove_empty_parents(mp3, self.settings.destination)
                except OSError as exc:
                    self.logger.warning("Move %s -> %s failed: %s", mp3, new_path, exc)
                    continue

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
                record.station_name, record.track.stream_title, str(new_path)
            )

            # Rewrite ID3 tags from enrichment so genre/year/album are embedded
            if info is not None:
                fallback_cover: bytes | None = None
                if self.settings.fallback_cover_path is not None:
                    with contextlib.suppress(OSError):
                        fallback_cover = self.settings.fallback_cover_path.read_bytes()
                try:
                    self.tagger.write_full(
                        new_path,
                        TrackInfo(
                            stream_title=record.track.stream_title,
                            artist=record.track.artist,
                            title=record.track.title,
                        ),
                        info,
                        None,
                        f"{record.station_name}@{record.track.stream_title}",
                        fallback_cover=fallback_cover,
                    )
                    self.logger.info(
                        "[%s] Rewrote enriched tags: album=%s year=%s genre=%s",
                        record.station_name,
                        info.album or "-",
                        info.year or "-",
                        info.genre or "-",
                    )
                except Exception as exc:
                    self.logger.debug("[%s] tag rewrite during reprocess: %s", record.station_name, exc)

            # --- same post-match flow as live recording ---
            recording_id = record.track.acoustid_recording_id
            score = record.track.acoustid_score
            if not recording_id and self.fingerprint is not None:
                if min_interval > 0:
                    now = time.monotonic()
                    wait = min_interval - (now - last_fp_call)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    last_fp_call = time.monotonic()
                try:
                    fp_result = await self.fingerprint.fingerprint(new_path)
                except Exception:
                    fp_result = None
                if fp_result:
                    recording_id = fp_result.recording_id
                    score = fp_result.score

            if recording_id:
                await apply_fingerprint_match(
                    recording_id=recording_id,
                    score=score or 0.0,
                    file_path=new_path,
                    new_path=new_path,
                    tagger=self.tagger,
                    cover_provider=self.cover_provider,
                    repository=self.repository,
                    station_name=record.station_name,
                    stream_title=record.track.stream_title,
                    logger=self.logger,
                    artist=record.track.artist,
                    title=record.track.title,
                    popularity_provider=self.popularity_provider,
                    min_popularity_rank=self.settings.min_popularity_rank,
                )

            count += 1
        self.logger.info("Reprocess-all: %d files processed.", count)

    async def reprocess_untested(self) -> None:
        """Re-fingerprint ``.untested.mp3`` files left from a previous run."""
        if self.fingerprint is None:
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
                self.logger.warning("Untested file missing, removing DB record: %s", p)
                with contextlib.suppress(Exception):
                    await self.repository.remove(rec.station_name, rec.track.stream_title)
                continue
            if min_interval > 0:
                now = time.monotonic()
                wait = min_interval - (now - last_fp_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                last_fp_call = time.monotonic()
            try:
                result = await self.fingerprint.fingerprint(p)
            except NonRetriableFingerprintError as exc:
                self.logger.warning(
                    "Permanently skipping broken file %s: %s",
                    p.name,
                    exc,
                )
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
                    remove_empty_parents(p, self.settings.destination)
                with contextlib.suppress(Exception):
                    await self.repository.remove(rec.station_name, rec.track.stream_title)
                continue
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
            applied = await apply_fingerprint_match(
                recording_id=result.recording_id,
                score=result.score,
                file_path=p,
                new_path=new_path,
                tagger=self.tagger,
                cover_provider=self.cover_provider,
                repository=self.repository,
                station_name=rec.station_name,
                stream_title=rec.track.stream_title,
                logger=self.logger,
                artist=result.artist,
                title=result.title,
                popularity_provider=self.popularity_provider,
                min_popularity_rank=self.settings.min_popularity_rank,
            )
            if applied is None:
                continue
        self.logger.info("Untested reprocess complete (%d files).", len(records))

    async def _cleanup_orphans(self) -> None:
        """Remove DB records whose ``file_path`` no longer exists on disk."""
        count = 0
        for rec in await self.repository.list_all():
            if not Path(rec.track.file_path).is_file():
                with contextlib.suppress(Exception):
                    await self.repository.remove(rec.station_name, rec.track.stream_title)
                self.logger.info(
                    "Removed orphan DB record (file missing): %s",
                    rec.track.file_path,
                )
                count += 1
        if count:
            self.logger.info("Orphan cleanup: removed %d stale records.", count)
        else:
            self.logger.debug("Orphan cleanup: no stale records found.")

    async def start(self) -> None:
        """Create and launch one :class:`StreamRecorder` task per stream."""
        await self._reprocess_all()
        await self.reprocess_untested()
        await self._cleanup_orphans()
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
                popularity_provider=self.popularity_provider,
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
