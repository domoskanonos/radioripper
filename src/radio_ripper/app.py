from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from pathlib import Path

from radio_ripper.infra.config import LiveConfig, Settings, StreamConfig
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver, load_local_m3u
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService, probe_icy
from radio_ripper.services.stream import StreamRecorder

_PROBE_TIMEOUT = 8.0
_PROBE_CONCURRENT = 20

_LOGGER = logging.getLogger("radio_ripper.app")


class RadioRipperApp:
    def __init__(
        self,
        *,
        settings: Settings,
        client: AsyncHttpClient,
        playlist_resolver: PlaylistResolver,
        live_config: LiveConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.resolver = playlist_resolver
        self._live_config = live_config
        self.logger = logger or _LOGGER
        self._recorders: list[StreamRecorder] = []
        self._cancel_requested = False
        self._housekeeping_task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(cls, settings: Settings, *, logger: logging.Logger | None = None) -> RadioRipperApp:
        log = logger or _LOGGER
        client = HttpxAsyncClient(user_agent=settings.user_agent)
        resolver = HttpPlaylistResolver(client, timeout=settings.request_timeout)
        return cls(
            settings=settings,
            client=client,
            playlist_resolver=resolver,
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
        client = HttpxAsyncClient(user_agent=settings.user_agent)
        resolver = HttpPlaylistResolver(client, timeout=settings.request_timeout)
        live_config = LiveConfig(config_path, settings)
        return cls(
            settings=settings,
            client=client,
            playlist_resolver=resolver,
            live_config=live_config,
            logger=log,
        )

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)

    def _select_stations(self) -> list[StreamConfig]:
        if self.settings.streams:
            return list(self.settings.streams)

        custom_path = self.settings.work_dir / "stations" / "custom.m3u"
        custom_stations = load_local_m3u(custom_path) if custom_path.is_file() else []
        if custom_stations:
            self.logger.info("Loaded %d stations from custom.m3u.", len(custom_stations))
        else:
            custom_path.parent.mkdir(parents=True, exist_ok=True)
            custom_path.write_text("#EXTM3U\n")
            self.logger.info("Created empty custom.m3u at %s.", custom_path)

        return list(custom_stations)

    async def start(self) -> None:
        if self._cancel_requested:
            self.logger.info("Startup cancelled.")
            return

        self._housekeeping_task = asyncio.create_task(self._run_housekeeping())

        stations = self._select_stations()
        has_explicit = bool(self.settings.streams)

        if not has_explicit:
            discovered = await PlaylistDiscoveryService(self.settings).load_or_discover()
            self.logger.info("Loaded %d stations via discovery.", len(discovered))
            stations.extend(discovered)

        if not stations:
            self.logger.error("No streams available. Exiting.")
            return

        stations = self._apply_stream_limit(stations)

        stations = await self._preflight_check(stations)
        if not stations:
            self.logger.error("No reachable streams. Exiting.")
            return

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
                logger=self.logger,
                ignore_title_patterns=patterns,
                no_icy_disable_after=self.settings.no_icy_disable_after,
                station_bitrate=stream.bitrate,
            )
            rec.start()
            self._recorders.append(rec)
        self.logger.info("Started %d stream recorders.", len(self._recorders))

    def _apply_stream_limit(self, stations: list[StreamConfig]) -> list[StreamConfig]:
        max_streams = self.settings.max_concurrent_streams
        if len(stations) <= max_streams:
            return stations
        custom_path = self.settings.work_dir / "stations" / "custom.m3u"
        custom_stations = load_local_m3u(custom_path) if custom_path.is_file() else []
        custom_count = len(custom_stations)
        if custom_count >= max_streams:
            return stations[:max_streams]
        return stations[:custom_count] + stations[custom_count:][: max_streams - custom_count]

    async def _preflight_check(self, stations: list[StreamConfig]) -> list[StreamConfig]:
        enabled = [s for s in stations if s.enabled]
        self.logger.info("Verifying reachability of %d station(s)...", len(enabled))
        sem = asyncio.Semaphore(_PROBE_CONCURRENT)
        probe_count = 0

        async def _check(s: StreamConfig) -> StreamConfig | None:
            nonlocal probe_count
            async with sem:
                probe_count += 1
                pct = probe_count * 100 // len(enabled)
                prev_pct = (probe_count - 1) * 100 // len(enabled)
                if pct != prev_pct and pct % 10 == 0:
                    self.logger.info("Probe progress: %d%% (%d/%d)", pct, probe_count, len(enabled))
                result = await probe_icy(str(s.url), timeout=_PROBE_TIMEOUT)
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

    def _count_inbox_files(self) -> int:
        inbox = self.settings.destination
        if not inbox.is_dir():
            return 0
        return sum(1 for _ in inbox.glob("*.mp3"))

    # ------------------------------------------------------------------ housekeeping

    async def _run_housekeeping(self) -> None:
        config_interval = 60.0
        inbox_interval = 300.0
        next_config = time.monotonic() + config_interval
        next_inbox = time.monotonic() + inbox_interval

        while not self._cancel_requested:
            now = time.monotonic()

            if now >= next_config and self._live_config is not None:
                diff = await self._live_config.check_reload()
                if diff:
                    self._apply_config_diff(diff)
                next_config = now + config_interval

            if now >= next_inbox:
                await self._check_inbox()
                next_inbox = now + inbox_interval

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._sleep_until(next_config, next_inbox), timeout=5.0)

    async def _sleep_until(self, *times: float) -> None:
        """Sleep until the earliest of *times or cancelled."""
        while not self._cancel_requested:
            earliest = min(t for t in times if t > 0) if times else float("inf")
            remaining = earliest - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 5.0))

    async def _check_inbox(self) -> None:
        count = self._count_inbox_files()
        limit = self.settings.max_files_inbox
        if count < limit:
            return

        self.logger.warning(
            "Inbox full (%d files >= %d) — pausing all recorders.",
            count,
            limit,
        )
        self._pause_all()
        resume_threshold = limit * 0.8
        while not self._cancel_requested:
            await asyncio.sleep(300.0)
            count = self._count_inbox_files()
            self.logger.info(
                "Inbox check: %d files (resume at ≤ %d).",
                count,
                resume_threshold,
            )
            if count <= resume_threshold:
                break
        if not self._cancel_requested:
            self.logger.info("Inbox has space — resuming all recorders.")
            self._resume_all()

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
        for rec in self._recorders:
            rec.stop()
        # Close HTTP client first — interrupts all in-flight stream connections
        await self.client.aclose()
        if self._recorders:
            tasks = [rec.join() for rec in self._recorders]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=15.0)
            except TimeoutError:
                self.logger.warning("Not all recorders stopped within 15s — continuing shutdown.")
        self.logger.info("All recorders stopped.")


__all__ = ["RadioRipperApp"]
