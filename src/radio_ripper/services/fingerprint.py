"""Audio fingerprinting providers (AcoustID / MusicBrainz).

The :class:`FingerprintProvider` ABC lets the ripper identify recorded files
against the AcoustID database. The default implementation uses ``pyacoustid``
which wraps the Chromaprint library.

A :class:`FingerprintError` is raised when fingerprinting fails for
*infrastructure* reasons (missing library, network error, API error, rate
limit). A return value of ``None`` from :meth:`fingerprint` strictly means
"the file was processed successfully but no match was found in the AcoustID
database" — callers may safely discard such files.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from radio_ripper.domain.models import FingerprintResult


class FingerprintError(RuntimeError):
    """Raised when fingerprinting fails for infrastructure reasons.

    This is distinct from a successful lookup that yields no match, which
    is signalled by :meth:`FingerprintProvider.fingerprint` returning
    ``None``.  Callers MUST NOT discard files when a :class:`FingerprintError`
    is raised — the failure is transient and the file should be retried
    later (e.g. by :meth:`RadioRipperApp.reprocess_untested`).
    """


class FingerprintProvider(ABC):
    """Identify a recorded audio file against the AcoustID database."""

    @abstractmethod
    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        """Return :class:`FingerprintResult` if the file matches a known recording.

        Raises:
            FingerprintError: if fingerprinting fails for infrastructure
                reasons (missing library, network error, API error).  A
                return value of ``None`` strictly means "successfully
                looked up but no match found".
        """


class AcoustidFingerprintProvider(FingerprintProvider):
    """AcoustID-backed fingerprint provider.

    Args:
        api_key: AcoustID API key.
        min_score: Minimum confidence score (0.0-1.0) to accept a match.
    """

    def __init__(self, api_key: str, *, min_score: float = 0.8) -> None:
        self._api_key = api_key
        self._min_score = min_score

    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        try:
            import acoustid  # type: ignore[import-untyped]
        except ImportError as exc:
            raise FingerprintError(
                "acoustid library not installed (pip install pyacoustid + system chromaprint)"
            ) from exc
        loop = asyncio.get_running_loop()
        try:
            gen = await loop.run_in_executor(None, acoustid.match, self._api_key, str(path))
            # acoustid.match returns a generator (parse_lookup_result uses yield).
            # Materialize it so we can subscript and len-check properly.
            # This also surfaces any WebServiceError raised during iteration.
            results = list(gen)
        except Exception as exc:
            raise FingerprintError(f"acoustid lookup failed: {exc}") from exc
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
    "FingerprintError",
    "FingerprintProvider",
    "NullFingerprintProvider",
]
