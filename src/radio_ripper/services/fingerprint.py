"""Audio fingerprinting providers (AcoustID / MusicBrainz).

The :class:`FingerprintProvider` ABC lets the ripper identify recorded files
against the AcoustID database. The default implementation uses ``pyacoustid``
which wraps the Chromaprint library.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from radio_ripper.domain.models import FingerprintResult


class FingerprintProvider(ABC):
    """Identify a recorded audio file against the AcoustID database."""

    @abstractmethod
    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        """Return :class:`FingerprintResult` if the file matches a known recording."""


class AcoustidFingerprintProvider(FingerprintProvider):
    """AcoustID-backed fingerprint provider.

    Args:
        api_key: AcoustID API key.
        min_score: Minimum confidence score (0.0–1.0) to accept a match.
    """

    def __init__(self, api_key: str, *, min_score: float = 0.8) -> None:
        self._api_key = api_key
        self._min_score = min_score

    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        try:
            import acoustid  # type: ignore[import-untyped]
        except ImportError:
            return None
        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                None, acoustid.match, self._api_key, str(path)
            )
        except Exception:
            return None
        if not results:
            return None
        best = results[0]
        score = float(best[0])
        if score < self._min_score:
            return None
        recording_id = str(best[1]) if best[1] else ""
        artist = str(best[2]) if best[2] else ""
        title = str(best[3]) if best[3] else ""
        if not artist and not title:
            return None
        return FingerprintResult(
            artist=artist,
            title=title,
            score=score,
            recording_id=recording_id,
        )


class NullFingerprintProvider(FingerprintProvider):
    """No-op provider used when AcoustID is disabled."""

    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        return None


__all__ = [
    "AcoustidFingerprintProvider",
    "FingerprintProvider",
    "NullFingerprintProvider",
]
