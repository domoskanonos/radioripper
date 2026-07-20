"""Playlist resolver — converts a playlist URL into a list of stream URLs.

ABC abstracts the resolver so we can swap remote (HTTP M3U/PLS) against local
files or static configurations in tests and future use cases.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from radio_ripper.infra.http import AsyncHttpClient


def parse_m3u(text: str) -> list[str]:
    """Parse M3U text content and return only valid http(s) URLs."""
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            urls.append(line)
    return urls


def parse_pls(text: str) -> list[str]:
    """Parse PLS text content and return only valid ``FileN`` URLs."""
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            _, _, value = line.partition("=")
            value = value.strip()
            if "://" in value:
                urls.append(value)
    return urls


class PlaylistResolver(ABC):
    """Resolve a playlist reference into a list of stream URLs."""

    @abstractmethod
    async def resolve(self, playlist_url: str) -> list[str]:
        """Return ordered stream URLs for ``playlist_url``."""


class HttpPlaylistResolver(PlaylistResolver):
    """HTTP-backed resolver supporting both M3U and PLS formats."""

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 30.0) -> None:
        self._client = client
        self._timeout = timeout

    async def resolve(self, playlist_url: str) -> list[str]:
        lower = playlist_url.lower()
        # If URL is a direct stream (not a playlist file), return it as-is
        if not (lower.endswith(".m3u") or lower.endswith(".pls") or lower.endswith(".m3u8")):
            return [playlist_url]
        # Otherwise fetch and parse the playlist
        text = await self._client.get_text(playlist_url, timeout=self._timeout)
        if lower.endswith(".pls") or "file" in text[:200].lower():
            return parse_pls(text)
        return parse_m3u(text)


class StaticPlaylistResolver(PlaylistResolver):
    """Resolver that just returns a pre-built list — useful for tests."""

    def __init__(self, urls: list[str]) -> None:
        self._urls = list(urls)

    async def resolve(self, playlist_url: str) -> list[str]:
        return list(self._urls)


__all__ = [
    "HttpPlaylistResolver",
    "PlaylistResolver",
    "StaticPlaylistResolver",
    "parse_m3u",
    "parse_pls",
]
