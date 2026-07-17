"""ID3v2 tagger built on top of :mod:`mutagen`.

:class:`TrackTagger` is the ABC, :class:`ID3Tagger` the default implementation.
Tags written:
    - ``TPE1``  (Artist)
    - ``TIT2``  (Title)
    - ``TALB``  (Album) — optional
    - ``TYER``  (Year) — optional
    - ``COMM``  (Recorded via Radio-Ripper)
    - ``TXXX:RIPPEDBY`` (station@playlist) — provenance
    - ``APIC``  (Cover art) — optional
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TXXX,
    ID3NoHeaderError,
)

from radio_ripper.domain.models import EnrichedInfo, TrackInfo
from radio_ripper.infra.errors import TaggingError


def _guess_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff") or b"JFIF" in data[:20]:
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


class TrackTagger(ABC):
    """Writes ID3 tags to a recorded MP3 file."""

    @abstractmethod
    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        """Write minimal tags (artist/title/comment) synchronously."""

    @abstractmethod
    def write_full(
        self,
        file_path: Path,
        track: TrackInfo,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        provenance: str,
    ) -> None:
        """Write enriched tags including album/year/genre and cover art."""


def _load_or_create(file_path: Path) -> ID3:
    """Load an existing ID3 tag or create a fresh one.

    Raises the underlying mutagen error (e.g. ``MutagenError``) if the file
    exists but cannot be read, or the file does not exist and the parent
    directory is missing.
    """
    try:
        return ID3(file_path)
    except ID3NoHeaderError:
        return ID3()


class ID3Tagger(TrackTagger):
    """mutagen-backed ID3 tagger."""

    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path}: {exc}") from exc
        audio.delall("TPE1")
        audio.delall("TIT2")
        audio.delall("COMM")
        audio.delall("TXXX:RIPPEDBY")
        if track.artist:
            audio.add(TPE1(encoding=3, text=track.artist))
        if track.title:
            audio.add(TIT2(encoding=3, text=track.title))
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via Radio-Ripper"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save basic tags to {file_path}: {exc}") from exc

    def write_full(
        self,
        file_path: Path,
        track: TrackInfo,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        provenance: str,
    ) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path}: {exc}") from exc
        audio.delall("TPE1")
        audio.delall("TIT2")
        audio.delall("TALB")
        audio.delall("TDRC")
        audio.delall("COMM")
        audio.delall("APIC")
        audio.delall("TXXX:RIPPEDBY")

        artist = enriched.artist or track.artist
        title = enriched.title or track.title
        if artist:
            audio.add(TPE1(encoding=3, text=artist))
        if title:
            audio.add(TIT2(encoding=3, text=title))
        if enriched.album:
            audio.add(TALB(encoding=3, text=enriched.album))
        if enriched.year:
            audio.add(TDRC(encoding=3, text=enriched.year))
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via Radio-Ripper"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))
        if cover_bytes:
            mime = _guess_image_mime(cover_bytes)
            audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_bytes))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save enriched tags to {file_path}: {exc}") from exc


class NullTagger(TrackTagger):
    """No-op tagger (used when tagging is disabled)."""

    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        return None

    def write_full(
        self,
        file_path: Path,
        track: TrackInfo,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        provenance: str,
    ) -> None:
        return None


__all__ = ["ID3Tagger", "NullTagger", "TrackTagger"]
