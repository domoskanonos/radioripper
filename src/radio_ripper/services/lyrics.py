"""Lyrics providers — fetch song lyrics from public APIs.

The :class:`LyricsProvider` ABC lets the ripper swap providers.
:class:`LyricsOvhProvider` uses the free `lyrics.ovh <https://lyrics.ovh>`_ API
which requires no API key.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from radio_ripper.infra.http import AsyncHttpClient

_log = logging.getLogger(__name__)

_LYRICS_OVH_URL = "https://api.lyrics.ovh/v1/{artist}/{title}"


class LyricsProvider(ABC):
    """Fetch lyrics text for a given artist + title."""

    @abstractmethod
    async def fetch(self, artist: str, title: str) -> str | None:
        """Return lyrics text or ``None`` when not found."""


class LyricsOvhProvider(LyricsProvider):
    """lyrics.ovh API — free, no API key required."""

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, artist: str, title: str) -> str | None:
        if not artist or not title:
            return None
        try:
            payload = await self._client.get_json(
                _LYRICS_OVH_URL.format(artist=artist, title=title),
                timeout=self._timeout,
            )
        except Exception:
            return None
        text: str | None = (payload or {}).get("lyrics")
        if text is not None:
            text = text.strip()
        return text or None


__all__ = ["LyricsOvhProvider", "LyricsProvider"]
