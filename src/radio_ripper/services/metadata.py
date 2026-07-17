"""Metadata enrichment providers.

The :class:`MetadataProvider` ABC lets the ripper swap iTunes for MusicBrainz,
Last.fm, etc. The current default is :class:`ITunesMetadataProvider` which uses
the public iTunes Search API (no API key required).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from radio_ripper.domain.models import EnrichedInfo
from radio_ripper.infra.http import AsyncHttpClient

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


class MetadataProvider(ABC):
    """Enrich track metadata (album, year, artwork) from an external source."""

    @abstractmethod
    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:
        """Return enriched info or ``None`` when no match is found."""

    @abstractmethod
    async def download_image(self, url: str) -> bytes | None:
        """Download cover-art bytes; ``None`` on failure."""


class ITunesMetadataProvider(MetadataProvider):
    """iTunes Search API metadata + cover art provider."""

    def __init__(
        self,
        client: AsyncHttpClient,
        *,
        metadata_timeout: float = 8.0,
        cover_timeout: float = 15.0,
    ) -> None:
        self._client = client
        self._metadata_timeout = metadata_timeout
        self._cover_timeout = cover_timeout

    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:
        query = f"{artist} {title}".strip()
        if not query:
            return None
        try:
            payload = await self._client.get_json(
                ITUNES_SEARCH_URL,
                params={"term": query, "limit": 1, "entity": "song", "media": "music"},
                timeout=self._metadata_timeout,
            )
        except Exception:
            return None
        results: list[dict[str, Any]] = (payload or {}).get("results") or []
        if not results:
            return None
        hit = results[0]
        artwork = hit.get("artworkUrl100") or hit.get("artworkUrl60")
        if artwork:
            artwork = self._upgrade_artwork(artwork)
        return EnrichedInfo(
            artist=hit.get("artistName"),
            title=hit.get("trackName"),
            album=hit.get("collectionName"),
            year=(hit.get("releaseDate") or "")[:4] or None,
            genre=hit.get("primaryGenreName"),
            artwork_url=artwork,
        )

    async def download_image(self, url: str) -> bytes | None:
        try:
            data = await self._client.get_bytes(url, timeout=self._cover_timeout)
        except Exception:
            return None
        if not data or len(data) < 64:
            return None
        return data

    @staticmethod
    def _upgrade_artwork(url: str) -> str:
        """Bump iTunes thumbnail to a higher resolution URL."""
        return (
            url.replace("100x100bb", "600x600bb")
               .replace("60x60bb", "600x600bb")
               .replace("100x100", "600x600")
               .replace("60x60", "600x600")
        )


class NullMetadataProvider(MetadataProvider):
    """No-op provider — used when enrichment is disabled in the config."""

    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:
        return None

    async def download_image(self, url: str) -> bytes | None:
        return None


__all__ = [
    "ITunesMetadataProvider",
    "MetadataProvider",
    "NullMetadataProvider",
]
