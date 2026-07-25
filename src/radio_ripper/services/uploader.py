"""Inbox-based MP3 uploader — polls mp3_inbox for files, fingerprints/enriches/tags/routes them."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from radio_ripper.domain.models import EnrichedInfo, FingerprintResult, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.services.fingerprint import (
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import compute_file_path
from radio_ripper.services.tagging import TrackTagger, enrich_and_tag


class Uploader:
    """Scans *mp3_inbox* for ``.mp3`` files, processes each through
    fingerprint → enrichment → ID3 tagging → route to destination.

    Files that match a known recording are enriched, tagged, and moved to
    :meth:`~radio_ripper.services.storage.compute_file_path`.  Unmatched
    or failed files are moved to *temp_dir*.  Corrupt files are deleted.
    """

    def __init__(
        self,
        inbox: Path,
        temp_dir: Path,
        settings: Settings,
        fingerprint_provider: FingerprintProvider,
        metadata_provider: MetadataProvider,
        repository: TrackRepository,
        tagger: TrackTagger,
        *,
        name: str = "inbox",
        poll_interval: float = 60.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._repo = repository
        self._tagger = tagger
        self._name = name
        self._poll_interval = poll_interval
        self._log = logger or logging.getLogger(__name__)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        self._log.info("Uploader started — polling %s every %.0fs", self._inbox, self._poll_interval)
        while not self._stop_event.is_set():
            try:
                await self._process_inbox()
            except Exception:
                self._log.exception("Uploader inbox scan failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
        self._log.info("Uploader stopped")

    async def _process_inbox(self) -> None:
        for mp3_path in sorted(self._inbox.glob("*.mp3")):
            if self._stop_event.is_set():
                return
            try:
                await self._process_one(mp3_path)
            except Exception:
                self._log.exception("Unexpected error processing %s — moving to temp", mp3_path)
                self._move_to_temp(mp3_path)

    async def _process_one(self, mp3_path: Path) -> None:
        proc_path = mp3_path.with_suffix(".processing")
        try:
            mp3_path.rename(proc_path)
        except OSError:
            self._log.warning("Cannot rename %s (concurrent access?) — skipping", mp3_path)
            return

        try:
            result = await self._fingerprint.fingerprint(proc_path)
        except NonRetriableFingerprintError:
            self._log.warning("Corrupt/unreadable %s — deleting", proc_path.name)
            self._cleanup_file(proc_path)
            return

        if result is None or not result.recording_id:
            self._log.info("No fingerprint match for %s — moving to temp", proc_path.name)
            self._move_to_temp(proc_path)
            return

        self._log.info("Matched %s → %s - %s (score=%.2f)", proc_path.name, result.artist, result.title, result.score)
        track_info = TrackInfo.from_stream_title(f"{result.artist} - {result.title}")
        stream_title = track_info.stream_title

        try:
            await self._repo.update_fingerprint(
                self._name, stream_title, recording_id=result.recording_id, score=result.score
            )
        except Exception:
            self._log.exception("Failed to update fingerprint record for %s", stream_title)

        enriched: EnrichedInfo | None = None
        if self._settings.enrich_metadata:
            enriched = await enrich_and_tag(
                self._metadata,
                self._tagger,
                proc_path,
                track_info,
                f"uploader/{self._name}",
                embed_cover_art=self._settings.embed_cover_art,
                logger=self._log,
            )

        if enriched and enriched.album:
            dest = compute_file_path(
                self._settings.destination,
                result.artist,
                result.title,
                track_info.title,
                album=enriched.album,
                overwrite=self._settings.overwrite_existing_files,
            )
            try:
                await self._repo.update_enrichment(
                    self._name, stream_title,
                    album=enriched.album,
                    year=enriched.year,
                    genre=enriched.genre,
                    label=enriched.label,
                    track_number=enriched.track_number,
                    disc_number=enriched.disc_number,
                )
            except Exception:
                self._log.exception("Failed to update enrichment record for %s", stream_title)
        else:
            dest = compute_file_path(
                self._settings.destination,
                result.artist,
                result.title,
                track_info.title,
                overwrite=self._settings.overwrite_existing_files,
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc_path.rename(dest)
            self._log.info("Filed %s → %s", stream_title, dest)
        except OSError:
            self._log.error("Cannot move %s → %s — copying instead", proc_path, dest)
            shutil.copy2(str(proc_path), str(dest))
            proc_path.unlink(missing_ok=True)

    def _move_to_temp(self, path: Path) -> None:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        dest = self._temp_dir / path.name
        try:
            shutil.move(str(path), str(dest))
            self._log.info("Moved %s → %s", path.name, dest)
        except OSError:
            self._log.exception("Failed to move %s to temp", path)

    def _cleanup_file(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self._log.exception("Cannot remove %s", path)


__all__ = ["Uploader"]
