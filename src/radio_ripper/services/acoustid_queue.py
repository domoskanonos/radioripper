"""AcoustID processing queue.

All successfully recorded MP3 files land in ``work_dir/unchecked_mp3/`` first.
This module manages a queue that picks them up, applies rate-limited AcoustID
lookups, and either moves them to the final destination (with correct
``Artist - Title.mp3`` naming) or discards them.

Design goals
------------
* **Strict fail-closed**: only a successful AcoustID lookup (score ≥ min_score
  *with* usable artist/title metadata) results in a kept file. Transient errors
  (network, API timeout, fpcalc crash) are retried with exponential back-off;
  the file stays in ``unchecked_mp3`` until the retry budget is exhausted.
* **Rate-limited**: a token-bucket style rate limiter enforces
  ``acoustid_requests_per_minute`` across all concurrent workers.
* **Persistent**: ``unchecked_mp3`` survives process restarts. On startup the
  app re-enqueues every ``*.mp3`` file found there.
* **Atomic rename + collision handling**: the final destination write is
  protected by ``_FINALIZE_LOCK`` so two concurrent workers can never clobber
  the same target. The recording with the higher AcoustID score wins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from radio_ripper.infra.config import Settings
from radio_ripper.services.storage import (
    _FINALIZE_LOCK,
    AcoustidLookup,
    acoustid_lookup,
    build_metadata_filename,
    get_mp3_duration,
    is_valid_mp3,
    read_mp3_score,
    write_mp3_tags,
)

if TYPE_CHECKING:
    from radio_ripper.infra.http import AsyncHttpClient

_LOGGER = logging.getLogger("radio_ripper.acoustid_queue")

# Sentinel used to signal the worker to stop
_STOP = object()


class _RateLimiter:
    """Evenly spaced rate limiter for AcoustID requests.

    A sliding-window limiter would permit a burst of ``rpm`` requests at the
    start of every minute. AcoustID documents a per-second limit, so requests
    are deliberately spaced at a fixed interval instead. The lock also makes
    the behavior correct if the queue gains more than one worker later.
    """

    def __init__(self, requests_per_minute: int) -> None:
        self._rpm = max(1, requests_per_minute)
        self._interval = 60.0 / self._rpm
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until we are allowed to make the next API request."""
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed = max(self._next_allowed, time.monotonic()) + self._interval


class AcoustidQueue:
    """Single-process AcoustID processing queue.

    Instantiate once per application, then:
    1. Call ``start()`` to launch the background worker task.
    2. Call ``enqueue(path)`` from any coroutine to add an unchecked file.
    3. Call ``stop()`` (and await) for graceful shutdown.

    The queue also exposes ``load_existing_unchecked()`` which scans
    ``work_dir/unchecked_mp3/`` for leftover files from previous runs and
    re-enqueues them.
    """

    UNCHECKED_DIR_NAME = "unchecked_mp3"

    def __init__(
        self,
        settings: Settings,
        api_key: str,
        destination: Path,
        http_client: AsyncHttpClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._api_key = api_key
        self._destination = destination
        self._http = http_client
        self._log = logger or _LOGGER
        self._unchecked_dir = settings.work_dir / self.UNCHECKED_DIR_NAME
        self._rate_limiter = _RateLimiter(settings.acoustid_requests_per_minute)
        # asyncio.Queue accepts both Path objects and the _STOP sentinel
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._retry_tasks: set[asyncio.Task[None]] = set()
        self._stopped = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def unchecked_dir(self) -> Path:
        return self._unchecked_dir

    def start(self) -> None:
        """Launch background worker (must be called inside a running event loop)."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopped = False
        self._unchecked_dir.mkdir(parents=True, exist_ok=True)
        self._worker_task = asyncio.create_task(self._worker_loop(), name="AcoustidQueue-worker")

    async def stop(self) -> None:
        """Stop workers without processing queued files during shutdown.

        Files already in ``unchecked_mp3`` are durable queue entries, so a
        shutdown should cancel the in-memory worker and leave those files for
        the next startup instead of waiting behind a potentially huge queue.
        """
        self._stopped = True
        for task in self._retry_tasks:
            task.cancel()
        if self._retry_tasks:
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._worker_task, timeout=30.0)
            self._worker_task = None

    def enqueue(self, path: Path) -> None:
        """Add a finished, validated MP3 to the processing queue.

        The file must already reside in ``unchecked_dir`` before calling this.
        If the unchecked directory is over the configured limits, the file is
        discarded and a warning is logged instead.
        """
        if self._stopped:
            return
        if not self._check_unchecked_limits(path):
            # Keep the file recoverable. The application can use this signal to
            # pause recording instead of losing an otherwise valid capture.
            self._log.warning("Unchecked directory over limit — retaining %s", path.name)
        self._queue.put_nowait(path)

    def load_existing_unchecked(self) -> int:
        """Re-enqueue every ``*.mp3`` file found in ``unchecked_dir``.

        Called once at application startup to recover files from previous runs.
        Files with a ``ACOUSTID_SCORE`` tag already set are also re-queued so
        their scoring is verified against current threshold (in case threshold
        changed).  Returns the number of files enqueued.
        """
        self._unchecked_dir.mkdir(parents=True, exist_ok=True)
        stale_parts = sorted(self._unchecked_dir.glob("*.part"))
        for part in stale_parts:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
        if stale_parts:
            self._log.info("Removed %d incomplete recording(s) from previous run.", len(stale_parts))
        files = sorted(self._unchecked_dir.glob("*.mp3"))
        for f in files:
            self._queue.put_nowait(f)
        if files:
            self._log.info("Re-enqueued %d unchecked file(s) from previous run.", len(files))
        return len(files)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_unchecked_limits(self, incoming: Path) -> bool:
        """Return True if limits are not exceeded, False if we should drop."""
        max_files = self._settings.max_unchecked_files
        max_bytes = self._settings.max_unchecked_bytes
        if max_files <= 0 and max_bytes <= 0:
            return True
        try:
            existing = list(self._unchecked_dir.glob("*.mp3"))
        except OSError:
            return True
        if max_files > 0 and len(existing) >= max_files:
            return False
        if max_bytes > 0:
            total = 0
            for file in existing:
                with contextlib.suppress(OSError):
                    total += file.stat().st_size
            if incoming not in existing:
                with contextlib.suppress(OSError):
                    total += incoming.stat().st_size
            if total >= max_bytes:
                return False
        return True

    async def _worker_loop(self) -> None:
        self._log.info("AcoustID queue worker started.")
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    break
                assert isinstance(item, Path)
                await self._process(item)
            except Exception:
                self._log.exception("Unexpected error in AcoustID worker.")
            finally:
                self._queue.task_done()
        self._log.info("AcoustID queue worker stopped.")

    async def _process(self, path: Path) -> None:
        """Run the full AcoustID pipeline for a single file.

        Retry transient errors (rate-limit / network / timeout) up to
        ``acoustid_retry_max_attempts`` times with exponential back-off.
        On permanent rejection (score too low, no metadata) delete the file.
        On success move it to ``destination`` with the correct name.
        """
        if not path.exists():
            self._log.debug("File gone before processing: %s", path.name)
            return

        local_result = await self._validate_local_file(path)
        if local_result is False:
            return
        if local_result is None:
            self._schedule_retry(path, self._settings.acoustid_retry_max_delay)
            return

        attempts = 0
        max_attempts = max(1, self._settings.acoustid_retry_max_attempts + 1)
        delay = self._settings.acoustid_retry_base_delay

        while attempts < max_attempts:
            attempts += 1

            # Honour the rate limit before every API call
            await self._rate_limiter.acquire()

            result = await acoustid_lookup(
                path,
                self._api_key,
                min_score=self._settings.acoustid_min_score,
                api_url=self._settings.acoustid_api_url,
                http_client=self._http,
            )

            if result.outcome == "accepted":
                if result.match is None:
                    self._log.error(
                        "AcoustID accepted %s without metadata; retaining for retry.",
                        path.name,
                    )
                    self._schedule_retry(path, self._settings.acoustid_retry_max_delay)
                    return
                committed = await self._commit(path, result)
                if not committed:
                    self._schedule_retry(path, self._settings.acoustid_retry_max_delay)
                return

            if result.outcome == "rejected":
                # Score too low or no match in AcoustID database — discard
                self._log.info(
                    "AcoustID rejected %s (%s) — deleting.",
                    path.name,
                    result.reject_reason or "score below threshold",
                )
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return

            # outcome == "error" — transient failure, retry
            self._log.warning(
                "AcoustID transient error for %s (attempt %d/%d): %s",
                path.name,
                attempts,
                max_attempts,
                result.error_detail or "unknown",
            )
            if attempts < max_attempts:
                actual_delay = min(delay, self._settings.acoustid_retry_max_delay)
                self._log.info("Retrying %s in %.0f s.", path.name, actual_delay)
                await asyncio.sleep(actual_delay)
                delay = min(delay * 2.0, self._settings.acoustid_retry_max_delay)

        # All retries exhausted — file stays in unchecked_dir for next run
        self._log.error(
            "AcoustID gave up on %s after %d attempt(s). File stays in unchecked_mp3 for next restart.",
            path.name,
            max_attempts,
        )
        self._schedule_retry(path, self._settings.acoustid_retry_max_delay)

    async def _validate_local_file(self, path: Path) -> bool | None:
        """Validate recovered files before spending an AcoustID request.

        Returns ``True`` for valid, ``False`` for permanent rejection, and
        ``None`` when a local dependency or read failed temporarily.
        """
        try:
            if path.stat().st_size < self._settings.min_file_size_bytes:
                self._log.info("AcoustID queue discarded too-small file: %s", path.name)
                path.unlink(missing_ok=True)
                return False
        except OSError as exc:
            self._log.warning("Could not stat %s: %s", path.name, exc)
            return None

        if not await is_valid_mp3(path):
            self._log.info("AcoustID queue discarded invalid MP3: %s", path.name)
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return False

        if self._settings.min_file_duration_s > 0:
            if shutil.which("ffprobe") is None:
                self._log.error("ffprobe unavailable; retaining %s for retry.", path.name)
                return None
            duration = await get_mp3_duration(path)
            if duration is None:
                self._log.warning("Could not determine duration for %s; retaining for retry.", path.name)
                return None
            if duration < self._settings.min_file_duration_s:
                self._log.info(
                    "AcoustID queue discarded too-short file: %s (%.1fs < %.1fs)",
                    path.name,
                    duration,
                    self._settings.min_file_duration_s,
                )
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return False
        return True

    def _schedule_retry(self, path: Path, delay: float) -> None:
        if self._stopped or not path.exists():
            return

        async def _retry() -> None:
            try:
                await asyncio.sleep(delay)
                if not self._stopped and path.exists():
                    self._queue.put_nowait(path)
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(_retry(), name=f"AcoustidRetry-{path.name}")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _commit(self, path: Path, result: AcoustidLookup) -> bool:
        """Write ID3 tags, build target name, handle collision, atomic move."""
        assert result.match is not None
        match = result.match

        target_name = build_metadata_filename(match.artist, match.title)
        if not target_name:
            # Should not happen if match has artist/title but be safe
            self._log.warning(
                "Cannot build filename for %s (%r — %r) — deleting.",
                path.name,
                match.artist,
                match.title,
            )
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return True

        target = self._destination / target_name

        # Serialise collision check + rename so concurrent workers don't race
        async with _FINALIZE_LOCK:
            if target.exists():
                existing_score = read_mp3_score(target)
                if existing_score is None:
                    # Do not make an unmetered second lookup here. Legacy
                    # unscored files are migrated to unchecked_mp3 at startup;
                    # if one remains, preserve it conservatively.
                    self._log.warning(
                        "Existing target %s has no AcoustID score; keeping it and discarding new duplicate.",
                        target.name,
                    )
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                    return True

                if existing_score is not None and existing_score >= match.score:
                    self._log.info(
                        "Kept existing %s (score %.2f >= %.2f) — discarding new recording.",
                        target.name,
                        existing_score,
                        match.score,
                    )
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                    return True

                if existing_score is not None:
                    self._log.info(
                        "Replacing %s (old score %.2f < %.2f).",
                        target.name,
                        existing_score,
                        match.score,
                    )
                else:
                    self._log.info(
                        "Replacing %s (existing score unknown — new score %.2f).",
                        target.name,
                        match.score,
                    )

            # Write ID3 tags into staging file, then atomic replace
            if not write_mp3_tags(path, artist=match.artist, title=match.title, score=match.score):
                self._log.error("Could not tag %s; retaining it in unchecked_mp3.", path.name)
                return False
            self._destination.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(str(path), str(target))
            except OSError as exc:
                self._log.error("Failed to move %s -> %s: %s", path.name, target.name, exc)
                return False

        self._log.info(
            "Accepted: %s (score=%.2f, %d bytes)",
            target.name,
            match.score,
            target.stat().st_size if target.exists() else 0,
        )
        return True


__all__ = ["AcoustidQueue"]
