"""Domain models — plain data carriers free of infrastructure concerns."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrackInfo:
    stream_title: str
    artist: str
    title: str

    @classmethod
    def from_stream_title(cls, stream_title: str) -> TrackInfo:
        stream_title = stream_title.strip()
        for sep in (" - ", " — "):
            if sep in stream_title:
                artist, _, song = stream_title.partition(sep)
                return cls(stream_title, artist.strip(), song.strip())
        return cls(stream_title, "", stream_title)


__all__ = ["TrackInfo"]
