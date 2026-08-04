from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path

from radio_ripper.infra.config import LiveConfig, Settings, StreamConfig
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.acoustid_queue import AcoustidQueue, cleanup_stale_parts
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService, probe_icy
from radio_ripper.services.storage import move_across_devices, read_mp3_score
from radio_ripper.services.stream import StreamRecorder

_LOGGER = logging.getLogger("radio_ripper.app")

_RELOAD_FIELDS = frozenset(Settings.model_fields) - frozenset({"log_level", "max_files_inbox"})


def _build_stream_client(settings: Settings) -> HttpxAsyncClient:
    """Build the shared streaming HTTP client with a pool that fits the stream count.

    All stream recorders share a single httpx client, so its connection pool must
    be at least as large as ``max_concurrent_streams``. Otherwise recorders beyond
    the pool size starve and fail with ``httpx.PoolTimeout``. ``http_pool_size``
    overrides the pool size explicitly (0 = follow ``max_concurrent_streams``).
    """
    pool_size = settings.http_pool_size if settings.http_pool_size > 0 else settings.max_concurrent_streams
    return HttpxAsyncClient(
        user_agent=settings.user_agent,
        max_pool_size=pool_size,
        pool_timeout=settings.http_pool_timeout,
        max_keepalive_connections=settings.http_max_keepalive,
    )


class RadioRipperApp:
    def __init__(
        self,
        *,
        settings: Settings,
        client: AsyncHttpClient,
        playlist_resolver: PlaylistResolver,
        live_config: LiveConfig | None = None,
        acoustid_api_key: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.resolver = playlist_resolver
        self._live_config = live_config
        self._acoustid_api_key = acoustid_api_key
        self.logger = logger or _LOGGER
        self._recorders: list[StreamRecorder] = []
        self._recorders_lock = asyncio.Lock()
        self._cancel_requested = False
        self._housekeeping_task: asyncio.Task[None] | None = None
        # True while recorders are paused because a storage/queue limit was hit
        self._backpressure_paused = False
        # AcoustID queue — created in start() once we have a running event loop
        self._acoustid_queue: AcoustidQueue | None = None
        # Dedicated small HTTP client for AcoustID (separate from stream pool)
        self._acoustid_http: HttpxAsyncClient | None = None

    @classmethod
    def from_settings(cls, settings: Settings, *, logger: logging.Logger | None = None) -> RadioRipperApp:
        log = logger or _LOGGER
        client = _build_stream_client(settings)
        resolver = HttpPlaylistResolver(client, timeout=settings.request_timeout)
        acoustid_key = os.environ.get("ACOUST_ID", "").strip()
        return cls(
            settings=settings,
            client=client,
            playlist_resolver=resolver,
            acoustid_api_key=acoustid_key,
            logger=log,
        )

    @classmethod
    def from_settings_with_live_config(
        cls,
        settings: Settings,
        config_path: str | Path,
        *,
        logger: logging.Logger | None = None,
    ) -> RadioRipperApp:
        log = logger or _LOGGER
        client = _build_stream_client(settings)
        resolver = HttpPlaylistResolver(client, timeout=settings.request_timeout)
        live_config = LiveConfig(config_path, settings)
        acoustid_key = os.environ.get("ACOUST_ID", "").strip()
        return cls(
            settings=settings,
            client=client,
            playlist_resolver=resolver,
            live_config=live_config,
            acoustid_api_key=acoustid_key,
            logger=log,
        )

    def recorders(self) -> list[StreamRecorder]:
        return list(self._recorders)

    async def start(self) -> None:
        if self._cancel_requested:
            self.logger.info("Startup cancelled.")
            return

        # 0) Sofort aufräumen: .part-Reste aus einem abgebrochenen Lauf entfernen
        cleanup_stale_parts(self.settings.work_dir)

        # 1) Housekeeping läuft sofort (Config-Reload, Backpressure)
        self._housekeeping_task = asyncio.create_task(self._run_housekeeping())

        # 2) Sender zuerst — Discovery + Preflight ist der langsamste Teil.
        #    Ohne erreichbare Sender wird das AcoustID-Setup komplett übersprungen.
        stations = await self._resolve_stations()
        if not stations:
            return

        # 3) Jetzt erst AcoustID-Pool + Queue einrichten
        await self._setup_acoustid_queue()

        # 4) Recorder starten
        await self._start_recorders(stations)

    async def _setup_acoustid_queue(self) -> None:
        """Create the AcoustID HTTP client, queue and worker; recover pending files."""
        if not self._acoustid_api_key:
            self.logger.warning(
                "ACOUST_ID not set — AcoustID fingerprinting disabled. "
                "Recordings stay in work/unchecked_mp3 and are not moved to destination."
            )
            return

        # Dedicated client with a small connection pool for AcoustID
        self._acoustid_http = HttpxAsyncClient(
            user_agent=self.settings.user_agent,
            max_pool_size=4,
            total_timeout=25.0,
        )
        self._acoustid_queue = AcoustidQueue(
            settings=self.settings,
            api_key=self._acoustid_api_key,
            destination=self.settings.destination,
            http_client=self._acoustid_http,
            logger=self.logger,
        )
        self._acoustid_queue.start()
        pending = self._acoustid_queue.load_existing_unchecked()
        if pending:
            self.logger.info("Found %d pending recording(s) in unchecked_mp3 from a previous run.", pending)
        # Migrate existing destination files that lack an ACOUSTID_SCORE tag
        await self._migrate_unscored_files()

    async def _migrate_unscored_files(self) -> None:
        """Move existing destination MP3s without ACOUSTID_SCORE tag to unchecked_mp3."""
        dest = self.settings.destination
        if not dest.is_dir() or self._acoustid_queue is None:
            return
        moved = 0
        for mp3 in dest.glob("*.mp3"):
            score = read_mp3_score(mp3)
            if score is None:
                try:
                    target = self._acoustid_queue.unchecked_dir / mp3.name
                    # Avoid clobbering if same name already in unchecked
                    if target.exists():
                        target = self._acoustid_queue.unchecked_dir / f"{mp3.stem}.{uuid.uuid4().hex}.mp3"
                    move_across_devices(mp3, target)
                    self._acoustid_queue.enqueue(target)
                    moved += 1
                except OSError as exc:
                    self.logger.warning("Could not migrate %s: %s", mp3.name, exc)
        if moved:
            self.logger.info("Migrated %d unscored destination file(s) to unchecked_mp3.", moved)

    async def _resolve_stations(self, *, context: str | None = None) -> list[StreamConfig]:
        stations = await PlaylistDiscoveryService(self.settings).load_or_discover()
        self.logger.info("Loaded %d stations via discovery.", len(stations))

        if not stations:
            self.logger.error("No streams available.%s", f" {context}." if context else " Exiting.")
            return []

        stations = self._apply_stream_limit(stations)

        stations = await self._preflight_check(stations)
        if not stations:
            self.logger.error("No reachable streams.%s", f" {context}." if context else " Exiting.")
        return stations

    async def _start_recorders(self, stations: list[StreamConfig]) -> None:
        async with self._recorders_lock:
            for stream in stations:
                if not stream.enabled:
                    self.logger.info("Skipping disabled stream: %s", stream.name)
                    continue
                p = stream.ignore_title_patterns
                patterns = p if p is not None else self.settings.ignore_title_patterns
                rec = StreamRecorder(
                    station_name=stream.name,
                    playlist_url=str(stream.url),
                    settings=self.settings,
                    http_client=self.client,
                    playlist_resolver=self.resolver,
                    acoustid_queue=self._acoustid_queue,
                    logger=self.logger,
                    ignore_title_patterns=patterns,
                    no_icy_disable_after=self.settings.no_icy_disable_after,
                    station_bitrate=stream.bitrate,
                )
                rec.start()
                self._recorders.append(rec)
            self.logger.info("Started %d stream recorders.", len(self._recorders))

    async def _stop_all_recorders(self) -> None:
        async with self._recorders_lock:
            if not self._recorders:
                return
            self.logger.info("Stopping %d stream recorders...", len(self._recorders))
            for rec in self._recorders:
                rec.stop()
            tasks = [rec.join() for rec in self._recorders]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=15.0)
            except TimeoutError:
                self.logger.warning("Not all recorders stopped within 15s — continuing.")
            self._recorders.clear()

    def _apply_stream_limit(self, stations: list[StreamConfig]) -> list[StreamConfig]:
        max_streams = self.settings.max_concurrent_streams
        if len(stations) <= max_streams:
            return stations
        return stations[:max_streams]

    async def _preflight_check(self, stations: list[StreamConfig]) -> list[StreamConfig]:
        enabled = [s for s in stations if s.enabled]
        self.logger.info("Verifying reachability of %d station(s)...", len(enabled))
        sem = asyncio.Semaphore(self.settings.probe_concurrent)
        probe_count = 0

        async def _check(s: StreamConfig) -> StreamConfig | None:
            nonlocal probe_count
            async with sem:
                probe_count += 1
                pct = probe_count * 100 // len(enabled)
                prev_pct = (probe_count - 1) * 100 // len(enabled)
                if pct != prev_pct and pct % 10 == 0:
                    self.logger.info("Probe progress: %d%% (%d/%d)", pct, probe_count, len(enabled))
                result = await probe_icy(str(s.url), timeout=self.settings.probe_timeout)
                if result.get("icy") and not result.get("error"):
                    return s
                reason = result.get("error") or "no ICY metadata"
                self.logger.error("[%s] Station unreachable: %s", s.name, reason)
                return None

        checked = await asyncio.gather(*[_check(s) for s in enabled])
        reachable = [s for s in checked if s is not None]
        unreachable = len(enabled) - len(reachable)
        if unreachable:
            self.logger.error(
                "%d of %d station(s) unreachable at startup, skipped.",
                unreachable,
                len(enabled),
            )
        disabled = [s for s in stations if not s.enabled]
        return disabled + reachable

    # ------------------------------------------------------------------ pause / resume

    def _pause_all(self) -> None:
        for rec in self._recorders:
            rec.pause()

    def _resume_all(self) -> None:
        for rec in self._recorders:
            rec.resume()

    def _staging_usage(self) -> tuple[int, int]:
        """Return ``(file_count, total_bytes)`` in work/unchecked_mp3."""
        staging = self.settings.work_dir / AcoustidQueue.UNCHECKED_DIR_NAME
        if not staging.is_dir():
            return 0, 0
        files = list(staging.glob("*.mp3"))
        total = 0
        for f in files:
            with contextlib.suppress(OSError):
                total += f.stat().st_size
        return len(files), total

    def _backpressure_reason(self) -> str | None:
        """Return a reason string when any storage limit is exceeded.

        The AcoustID queue *is* ``work/unchecked_mp3``, so it is covered by the
        staging file/byte limits below. Checks, in order: destination file
        count, staging file count, staging byte count. Returns ``None`` when
        everything is within limits.
        """
        if self.settings.destination.is_dir():
            dest_count = sum(1 for _ in self.settings.destination.glob("*.mp3"))
            if dest_count >= self.settings.max_files_inbox:
                return f"destination {dest_count} >= max_files_inbox {self.settings.max_files_inbox}"

        staging_count, staging_bytes = self._staging_usage()
        if self.settings.max_unchecked_files > 0 and staging_count >= self.settings.max_unchecked_files:
            return f"unchecked_mp3 files {staging_count} >= max_unchecked_files {self.settings.max_unchecked_files}"
        if self.settings.max_unchecked_bytes > 0 and staging_bytes >= self.settings.max_unchecked_bytes:
            return f"unchecked_mp3 bytes {staging_bytes} >= max_unchecked_bytes {self.settings.max_unchecked_bytes}"

        return None

    def _backpressure_cleared(self) -> bool:
        """Return True when every limit is back below its resume threshold (80 %)."""
        if self.settings.destination.is_dir():
            dest_count = sum(1 for _ in self.settings.destination.glob("*.mp3"))
            if dest_count > self.settings.max_files_inbox * 0.8:
                return False

        staging_count, staging_bytes = self._staging_usage()
        over_files = self.settings.max_unchecked_files > 0 and staging_count > self.settings.max_unchecked_files * 0.8
        over_bytes = self.settings.max_unchecked_bytes > 0 and staging_bytes > self.settings.max_unchecked_bytes * 0.8
        return not (over_files or over_bytes)

    # ------------------------------------------------------------------ housekeeping

    async def _run_housekeeping(self) -> None:
        config_interval = 60.0
        backpressure_interval = 30.0
        next_config = time.monotonic() + config_interval
        next_backpressure = time.monotonic() + backpressure_interval

        while not self._cancel_requested:
            now = time.monotonic()

            if now >= next_config:
                await self._process_config_reload()
                next_config = now + config_interval

            if now >= next_backpressure:
                await self._check_backpressure()
                next_backpressure = now + backpressure_interval

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sleep_until(next_config, next_backpressure), timeout=5.0)

    async def _process_config_reload(self) -> None:
        lc = self._live_config
        if lc is None:
            return
        diff = await lc.check_reload()
        if not diff:
            return
        # Update app.settings to the newly created Settings instance
        self.settings = lc.settings
        # Keep the AcoustID queue on the live settings (rate limit, score, ...)
        if self._acoustid_queue is not None:
            self._acoustid_queue.update_settings(self.settings)
        self._apply_config_diff(diff)
        if _RELOAD_FIELDS.intersection(diff):
            await self._reload_after_config_change()

    async def _sleep_until(self, *times: float) -> None:
        """Sleep until the earliest of *times or cancelled."""
        while not self._cancel_requested:
            earliest = min(t for t in times if t > 0) if times else float("inf")
            remaining = earliest - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 5.0))

    async def _check_backpressure(self) -> None:
        """Pause/resume all recorders based on storage and queue capacity.

        Pauses as soon as *any* limit is hit — destination file count, staging
        dir file/byte count or the in-memory AcoustID queue size. Resumes once
        every limit is back below 80 % of its threshold.
        """
        if self._cancel_requested:
            return

        if self._backpressure_paused:
            if self._backpressure_cleared():
                self.logger.info("Backpressure cleared — resuming all recorders.")
                self._resume_all()
                self._backpressure_paused = False
            return

        reason = self._backpressure_reason()
        if reason is None:
            return

        self.logger.warning("Backpressure: %s — pausing all recorders.", reason)
        self._pause_all()
        self._backpressure_paused = True

    def _apply_config_diff(self, diff: dict[str, tuple]) -> None:
        lc = self._live_config
        assert lc is not None
        for field, (old, new) in diff.items():
            self.logger.info("Config changed: %s = %r (was %r)", field, new, old)
        if "log_level" in diff:
            logging.getLogger("radio_ripper").setLevel(getattr(logging, lc.settings.log_level))
        if "max_files_inbox" in diff:
            self.logger.info(
                "Inbox limit changed — next check uses new threshold (%d).",
                lc.settings.max_files_inbox,
            )

    async def _reload_after_config_change(self) -> None:
        if self._cancel_requested:
            return
        self.logger.info("Config changed — restarting stream recorders.")
        await self._stop_all_recorders()
        stations = await self._resolve_stations(context="after config reload")
        if not stations:
            return
        await self._start_recorders(stations)

    # ------------------------------------------------------------------ lifecycle

    def cancel(self) -> None:
        self._cancel_requested = True

    async def stop(self) -> None:
        self._cancel_requested = True
        if self._housekeeping_task is not None:
            self._housekeeping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._housekeeping_task
            self._housekeeping_task = None
        self.logger.info("Stopping all recorders...")
        async with self._recorders_lock:
            for rec in self._recorders:
                rec.stop()
        # Stop recorders BEFORE closing HTTP clients to prevent connection leaks
        await self._stop_all_recorders()
        await self.client.aclose()
        # Stop AcoustID queue after recorders so no new items arrive during shutdown
        if self._acoustid_queue is not None:
            await self._acoustid_queue.stop()
        if self._acoustid_http is not None:
            await self._acoustid_http.aclose()
        self.logger.info("All recorders stopped.")


__all__ = ["RadioRipperApp"]
