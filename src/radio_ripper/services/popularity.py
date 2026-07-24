"""Popularity check via public Deezer API.

Used to delete tracks that don't meet a minimum popularity threshold.
No API key required — Deezer's search endpoint is public.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from radio_ripper.infra.http import AsyncHttpClient
from radio_ripper.services.repository import TrackRepository

_LOGGER = logging.getLogger("radio_ripper.popularity")
_DELAY = 0.2


class DeezerPopularityChecker:
    """Check track popularity via the public Deezer search API.

    Returns a ``rank`` (0 — ~1M) per track. Higher = more popular.
    ``None`` when no match or the API is unreachable.
    """

    _SEARCH_URL = "https://api.deezer.com/search"

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout

    async def get_rank(self, artist: str, title: str) -> int | None:
        q = f'{artist} "{title}"'
        try:
            payload = await self._client.get_json(
                self._SEARCH_URL, params={"q": q, "limit": 1}, timeout=self._timeout
            )
        except Exception:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            return None
        rank = data[0].get("rank")
        return int(rank) if rank is not None else None


async def maybe_delete_obscure(
    *,
    file_path: Path,
    station_name: str,
    stream_title: str,
    artist: str,
    title: str,
    min_rank: int,
    popularity_provider: DeezerPopularityChecker | None,
    repository: TrackRepository,
    logger: logging.Logger = _LOGGER,
) -> bool:
    """Check popularity and delete *file_path* + DB record if below threshold.

    Returns ``True`` when the file was deleted.
    Best-effort — failures are logged and never reraised.
    """
    if min_rank <= 0 or popularity_provider is None:
        return False
    if not artist and not title:
        return False

    rank = await popularity_provider.get_rank(artist, title)
    if rank is None:
        return False

    logger.info(
        "[%s] Popularity rank %s — %s / %s = %d",
        station_name,
        "DELETED" if rank < min_rank else "OK",
        artist,
        title,
        rank,
    )

    if rank >= min_rank:
        return False

    with contextlib.suppress(OSError):
        file_path.unlink(missing_ok=True)
    try:
        await repository.remove(station_name, stream_title)
    except Exception as exc:
        logger.debug("[%s] db remove after popularity delete: %s", station_name, exc)

    logger.warning(
        "[%s] Deleted obscure track (rank=%d < min=%d): %s",
        station_name,
        rank,
        min_rank,
        file_path.name,
    )
    return True


__all__ = ["DeezerPopularityChecker", "maybe_delete_obscure"]
