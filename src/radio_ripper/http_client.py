"""http_client.py — Async HTTP-Client für Streaming und Playlists."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol

import httpx


class PlaylistTextClient(Protocol):
    """Minimales Interface für Playlist-Auflösung (nur ``get_text``)."""

    async def get_text(self, url: str, *, timeout: float | None = None) -> str: ...


class HttpxClient:
    """Vereinfachter async HTTP-Client für Streaming und Playlists."""

    def __init__(
        self,
        *,
        user_agent: str = "VLC/3.0.18 LibVLC/3.0.18",
        max_pool_size: int = 500,
        total_timeout: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(total_timeout, connect=10.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=max_pool_size,
                max_keepalive_connections=min(100, max_pool_size),
            ),
        )
        self._last_headers: dict[str, str] = {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text

    async def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[bytes, None]:
        async with self._client.stream("GET", url, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            self._last_headers = dict(resp.headers)
            async for chunk in resp.aiter_bytes():
                yield chunk

    def response_headers(self) -> dict[str, str]:
        return dict(self._last_headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpxClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


async def resolve_playlist(
    client: PlaylistTextClient,
    playlist_url: str,
    *,
    timeout: float = 30.0,
) -> list[str]:
    """Löst eine Playlist-URL in eine Liste von Stream-URLs auf.

    Ist die URL keine M3U-Playlist, wird sie direkt als Stream-URL
    zurückgegeben.
    """
    if not playlist_url.lower().endswith((".m3u", ".m3u8")):
        return [playlist_url]
    text = await client.get_text(playlist_url, timeout=timeout)
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "://" in line:
            urls.append(line)
    return urls
