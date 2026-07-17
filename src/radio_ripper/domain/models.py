"""Domain models — plain data carriers free of infrastructure concerns."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """Immutable track description parsed from the ICY StreamTitle.

    Attributes:
        stream_title: Raw original ``StreamTitle`` string.
        artist: Best-effort artist segmentation, or empty string if not separable.
        title: Song title portion (or entire stream_title if no separator found).
    """

    stream_title: str
    artist: str
    title: str

    @classmethod
    def from_stream_title(cls, stream_title: str) -> TrackInfo:
        """Create a :class:`TrackInfo` by splitting ``Artist - Title``."""
        stream_title = stream_title.strip()
        for sep in (" - ", " — "):
            if sep in stream_title:
                artist, _, song = stream_title.partition(sep)
                return cls(stream_title, artist.strip(), song.strip())
        return cls(stream_title, "", stream_title)


@dataclass(frozen=True, slots=True)
class EnrichedInfo:
    """Metadata enrichment results fetched from external providers (e.g. iTunes).

    All fields are optional — providers may only fill part of them.
    """

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: str | None = None
    genre: str | None = None
    artwork_url: str | None = None


@dataclass(slots=True)
class SavedTrack:
    """A track that has been successfully recorded and written to disk."""

    stream_title: str
    artist: str
    title: str
    file_path: str
    file_size: int
    album: str | None = None
    year: str | None = None
    has_cover: bool = False
    enrichment: str | None = None
    extras: dict[str, str] = field(default_factory=dict)


__all__ = ["EnrichedInfo", "SavedTrack", "TrackInfo"]