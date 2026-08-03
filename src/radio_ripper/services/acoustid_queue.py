"""AcoustID processing queue — ``work_dir/unchecked_mp3`` IS the queue.

Recordings land directly in ``work_dir/unchecked_mp3/`` (written there by the
recorder, committed atomically from ``.part``). There is deliberately no second
in-memory queue: a single worker scans the directory, processes the *oldest*
file first (by modification/creation time), runs a rate-limited AcoustID lookup
and either moves the file to ``destination`` (with correct ``Artist - Title.mp3``
naming) or discards it.

Design goals
------------
* **unchecked_mp3 is the queue** — durable, survives restarts, sorted oldest
  first. ``enqueue()`` only wakes the worker; it never buffers.
* **Strict fail-closed**: only a successful AcoustID lookup (score ≥ min_score
  *with* usable artist/title metadata) results in a kept file. Transient errors
  are retried with exponential back-off; the file stays in ``unchecked_mp3``.
* **Rate-limited**: requests are evenly spaced to stay within
  ``acoustid_requests_per_minute`` (AcoustID hard limit: 180/min = 3 req/s).
* **Atomic rename + collision handling**: the final destination write is
  protected by ``_FINALIZE_LOCK`` so concurrent workers can never clobber the
  same target. The recording with the higher AcoustID score wins.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    move_across_devices,
    read_mp3_score,
    write_mp3_tags,
)

if TYPE_CHECKING:
    from radio_ripper.infra.http import AsyncHttpClient

_LOGGER = logging.getLogger("radio_ripper.acoustid_queue")


def cleanup_stale_parts(work_dir: Path) -> int:
    """Remove leftover ``.part`` files from a crashed/previous run.

    ``.part`` files are incomplete recordings (the atomic rename to ``.mp3``
    never happened). They are garbage and would never be processed, so they are
    deleted. Returns the number of removed files.
    """
    staging = work_dir / AcoustidQueue.UNCHECKED_DIR_NAME
    if not staging.is_dir():
        return 0
    parts = sorted(staging.glob("*.part"))
    for part in parts:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
    if parts:
        _LOGGER.info("Removed %d incomplete recording(s) (.part) from a previous run.", len(parts))
    return len(parts)


class _RateLimiter:
    """Evenly spaced rate limiter for AcoustID requests.

    A sliding-window limiter would permit a burst of ``rpm`` requests at the
    start of every minute. AcoustID documents a per-second limit, so requests
    are deliberately spaced at a fixed interval instead. The rate is read live
    from the Settings object, so hot-reloading ``acoustid_requests_per_minute``
    takes effect without a restart.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    def update(self, settings: Settings) -> None:
        """Adopt a new Settings object so the rate can change live."""
        self._settings = settings

    @property
    def _interval(self) -> float:
        return 60.0 / max(1, self._settings.acoustid_requests_per_minute)

    async def acquire(self) -> None:
        """Block until we are allowed to make the next API request."""
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_allowed = max(self._next_allowed, time.monotonic()) + self._interval


class AcoustidQueue:
    """Single-process AcoustID worker over ``work_dir/unchecked_mp3``.

    Instantiate once per application, then:
    1. Call ``start()`` to launch the background worker task.
    2. Call ``enqueue(path)`` from any coroutine once a file is committed.
    3. Call ``stop()`` (and await) for graceful shutdown.

    The worker scans ``unchecked_mp3`` itself, so files left behind by a
    previous run are picked up automatically on startup.
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
        self._rate_limiter = _RateLimiter(settings)
        self._worker_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._wake_event = asyncio.Event()
        # path -> (next_retry_monotonic, attempts) for files in cooldown/back-off
        self._retry_info: dict[Path, tuple[float, int]] = {}
        self._idle_sleep = 2.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def unchecked_dir(self) -> Path:
        return self._unchecked_dir

    def update_settings(self, settings: Settings) -> None:
        """Adopt a new Settings object (hot-reload).

        The rate limiter, score threshold, API URL and retry/limit settings are
        all read from the live Settings instance.
        """
        self._settings = settings
        self._rate_limiter.update(settings)

    def start(self) -> None:
        """Launch background worker (must be called inside a running event loop)."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopped = False
        self._unchecked_dir.mkdir(parents=True, exist_ok=True)
        self._worker_task = asyncio.create_task(self._worker_loop(), name="AcoustidQueue-worker")

    async def stop(self) -> None:
        """Stop the worker; files stay durable in ``unchecked_mp3``.

        Because the directory is the queue, an unfinished shutdown simply leaves
        files there for the next startup — nothing is lost and nothing is
        buffered in memory.
        """
        self._stopped = True
        self._wake_event.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._worker_task, timeout=30.0)
            self._worker_task = None

    def enqueue(self, path: Path) -> None:
        """Signal that a new file is available in ``unchecked_mp3``.

        The file is already durably stored there; this only wakes the worker so
        it does not have to poll. The file is never deleted here, even when the
        staging directory is over its configured limits — pausing recording is
        the application's backpressure concern.
        """
        if self._stopped:
            return
        if not self._check_unchecked_limits(path):
            self._log.warning("Unchecked directory over limit — retaining %s", path.name)
        self._wake_event.set()

    def load_existing_unchecked(self) -> int:
        """Clean stale ``.part`` files and report pending recordings.

        The worker picks up existing files naturally by scanning the directory,
        so nothing needs to be re-queued. Returns the number of pending MP3s.
        """
        self._unchecked_dir.mkdir(parents=True, exist_ok=True)
        cleanup_stale_parts(self._settings.work_dir)
        pending = sorted(self._unchecked_dir.glob("*.mp3"))
        if pending:
            self._log.info("Found %d pending recording(s) in unchecked_mp3.", len(pending))
            self._wake_event.set()
        return len(pending)

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        self._log.info("AcoustID queue worker started.")
        while not self._stopped:
            try:
                target = self._pick_next()
            except asyncio.CancelledError:
                break
            if target is None:
                # Nothing ready right now — sleep until the earliest retry or
                # until a new file wakes us.
                self._wake_event.clear()
                if self._pick_next() is not None:
                    continue
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self._cooldown_wait())
                continue
            try:
                await self._process(target)
            except Exception:
                self._log.exception("Unexpected error processing %s", target.name)
                self._schedule_retry(target, self._settings.acoustid_retry_base_delay)
        self._log.info("AcoustID queue worker stopped.")

    def _pick_next(self) -> Path | None:
        """Return the oldest pending file whose retry cooldown has elapsed."""
        for p in [p for p in self._retry_info if not p.exists()]:
            self._retry_info.pop(p, None)
        now = time.monotonic()
        try:
            files = sorted(self._unchecked_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return None
        for f in files:
            info = self._retry_info.get(f)
            if info is not None and info[0] > now:
                continue
            return f
        return None

    def _cooldown_wait(self) -> float:
        """Seconds to sleep until the earliest pending retry (or idle poll)."""
        if not self._retry_info:
            return self._idle_sleep
        now = time.monotonic()
        pending = [t for t, _ in self._retry_info.values() if t > now]
        if not pending:
            return self._idle_sleep
        return min(min(pending) - now, 60.0)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    async def _process(self, path: Path) -> None:
        """Run the full AcoustID pipeline for a single file (one attempt)."""
        if not path.exists():
            self._retry_info.pop(path, None)
            return

        local_result = await self._validate_local_file(path)
        if local_result is False:
            self._retry_info.pop(path, None)
            return
        if local_result is None:
            self._schedule_retry(path, self._settings.acoustid_retry_max_delay)
            return

        await self._rate_limiter.acquire()

        result = await acoustid_lookup(
            path,
            self._api_key,
            min_score=self._settings.acoustid_min_score,
            api_url=self._settings.acoustid_api_url,
            http_client=self._http,
        )

        if result.outcome == "accepted":
            # acoustid_lookup() already rejects matches without usable
            # artist/title metadata, so a match is guaranteed here.
            committed = await self._commit(path, result)
            if committed:
                self._retry_info.pop(path, None)
            else:
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
            self._retry_info.pop(path, None)
            return

        # outcome == "error" — transient failure, retry later
        self._log.warning(
            "AcoustID transient error for %s: %s",
            path.name,
            result.error_detail or "unknown",
        )
        self._schedule_retry(path, self._settings.acoustid_retry_base_delay)

    async def _validate_local_file(self, path: Path) -> bool | None:
        """Validate files before spending an AcoustID request.

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

    def _schedule_retry(self, path: Path, base_delay: float) -> None:
        """Record a retry for *path* with exponential back-off.

        The retry is enforced by ``_pick_next`` skipping files whose cooldown
        has not elapsed. Once ``acoustid_retry_max_attempts`` is exhausted the
        file simply stays in ``unchecked_mp3`` for the next restart.
        """
        if self._stopped or not path.exists():
            return
        _, attempts = self._retry_info.get(path, (0, 0))
        max_attempts = max(1, self._settings.acoustid_retry_max_attempts + 1)
        if attempts >= max_attempts:
            self._log.error(
                "AcoustID gave up on %s after %d attempt(s); file stays in unchecked_mp3 for next restart.",
                path.name,
                max_attempts,
            )
            self._retry_info.pop(path, None)
            return
        attempts += 1
        backoff = min(base_delay * (2 ** (attempts - 1)), self._settings.acoustid_retry_max_delay)
        self._retry_info[path] = (time.monotonic() + backoff, attempts)
        self._log.info("Retrying %s in %.0f s (attempt %d/%d).", path.name, backoff, attempts, max_attempts)

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

            # Write ID3 tags into staging file, then move it to its destination
            # (atomic rename on one device; copy+unlink fallback across mounts).
            if not write_mp3_tags(path, artist=match.artist, title=match.title, score=match.score):
                self._log.error("Could not tag %s; retaining it in unchecked_mp3.", path.name)
                return False
            self._destination.mkdir(parents=True, exist_ok=True)
            try:
                move_across_devices(path, target)
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


__all__ = ["AcoustidQueue", "cleanup_stale_parts"]
