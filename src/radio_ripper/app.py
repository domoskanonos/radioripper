"""Application orchestrator — stream mode.

Creates stream recorders for each station, dumps raw MP3 files into
``work/streaming_results/``. No tagging, no enrichment, no database.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.playlist import HttpPlaylistResolver, PlaylistResolver, load_local_m3u
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService
from radio_ripper.services.stream import StreamRecorder

_LOGGER = logging.getLogger("radio_ripper.app")


class RadioRipperApp:
    """Compose stream recorders and run them concurrently.

    Intended for use with the ``stream`` subcommand — records raw MP3s
    and dumps them into ``work/streaming_results/`` for later processing
    by the ``tag`` subcommand.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: AsyncHttpClient,
        playlist_resolver: PlaylistResolver,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.resolver = playlist_resolver
        self.logger = logger or _LOGGER
        self._recorders: list[StreamRecorder] = []
        self._cancel_requested = False

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

    def recorders(self) -> Sequence[StreamRecorder]:
        return list(self._recorders)

    async def start(self) -> None:
        if self._cancel_requested:
            self.logger.info("Startup cancelled.")
            return

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

            max_streams = self.settings.max_concurrent_streams
            if len(stations) > max_streams:
                custom_count = len(custom_stations)
                if custom_count >= max_streams:
                    stations = stations[:max_streams]
                else:
                    stations = stations[:custom_count] + stations[custom_count:][:max_streams - custom_count]

            self.settings.streams = stations

        for stream in self.settings.streams:
            if not stream.enabled:
                self.logger.info("Skipping disabled stream: %s", stream.name)
                continue
            patterns = (
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
                logger=self.logger,
                ad_title_patterns=patterns,
                no_icy_disable_after=self.settings.no_icy_disable_after,
            )
            rec.start()
            self._recorders.append(rec)
        self.logger.info("Started %d stream recorders.", len(self._recorders))

    def cancel(self) -> None:
        self._cancel_requested = True

    async def stop(self) -> None:
        self._cancel_requested = True
        self.logger.info("Stopping all recorders...")
        for rec in self._recorders:
            rec.stop()
        for rec in self._recorders:
            try:
                await asyncio.wait_for(rec.join(), timeout=10.0)
            except TimeoutError:
                self.logger.warning("Recorder %s did not stop in time.", rec.station_name)
        await self.client.aclose()
        self.logger.info("All recorders stopped.")


__all__ = ["RadioRipperApp"]
