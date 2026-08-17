from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import HttpUrl

from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.infra.errors import InvalidUrlError
from radio_ripper.infra.validation import validate_stream_url
from radio_ripper.services.m3u_parser import M3uEntry, parse_m3u_entries

_LOGGER = logging.getLogger("radio_ripper.discovery")


async def probe_icy(
    url: str,
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    result: dict[str, Any] = {"icy": False, "bitrate": 0, "error": None}

    try:
        url = validate_stream_url(url)
    except InvalidUrlError as e:
        result["error"] = f"invalid URL: {e}"
        return result

    headers = {"Icy-MetaData": "1", "User-Agent": "Radio-Ripper/2.0"}

    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            ) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            if resp.status_code not in (200, 206):
                result["error"] = f"HTTP {resp.status_code}"
                return result
            resp_headers = dict(resp.headers)
            ct = resp_headers.get("content-type", "").lower()
            if ct and ct != "audio/mpeg":
                result["error"] = f"not MP3 ({ct})"
                return result
            metaint = resp_headers.get("icy-metaint") or resp_headers.get("Icy-Metaint")
            result["icy"] = metaint is not None
            br_raw = resp_headers.get("icy-br") or resp_headers.get("Icy-Br")
            if br_raw:
                with contextlib.suppress(ValueError, TypeError):
                    result["bitrate"] = int(br_raw)
            try:
                async for chunk in resp.aiter_bytes():
                    result["read_bytes"] = len(chunk)
                    break
            except Exception as exc:
                result["error"] = f"no data: {exc!s}"[:60]
                return result
    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connect"
    except httpx.RemoteProtocolError:
        result["error"] = "protocol"
    except Exception as exc:
        result["error"] = str(exc)[:60]
    return result


class PlaylistDiscoveryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = _LOGGER

    async def load_or_discover(self) -> list[StreamConfig]:
        custom_m3u = self._settings.work_dir / "stations" / "custom.m3u"
        if not custom_m3u.is_file():
            self._log.warning("Keine custom.m3u gefunden: %s", custom_m3u)
            return []

        text = custom_m3u.read_text("utf-8")
        entries = parse_m3u_entries(text, source="custom")

        stations: list[StreamConfig] = []
        for e in entries:
            try:
                stations.append(
                    StreamConfig(
                        name=e.name[:64],
                        url=e.url,
                        enabled=True,
                        bitrate=0,
                        icy=True,
                        source=e.source,
                    )
                )
            except Exception as exc:
                self._log.warning("Ungültiger Eintrag %s: %s", e.name, exc)

        self._log.info("%d Stationen aus custom.m3u geladen", len(stations))
        return stations


__all__ = [
    "M3uEntry",
    "PlaylistDiscoveryService",
    "parse_m3u_entries",
    "probe_icy",
]