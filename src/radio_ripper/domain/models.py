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


@dataclass(frozen=True, slots=True)
class Station:
    name: str
    url: str
    bitrate: int = 0
    source: str = ""


@dataclass(frozen=True, slots=True)
class StreamMetadata:
    stream_title: str
    artist: str
    title: str
    metaint: int = 0
    bitrate: int = 0


__all__ = ["Station", "StreamMetadata", "TrackInfo"]
