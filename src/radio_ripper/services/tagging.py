# mypy: disable-error-code="no-untyped-call"
"""ID3v2 tagger built on top of :mod:`mutagen`.

:class:`TrackTagger` is the ABC, :class:`ID3Tagger` the default implementation.
Tags written:
    - ``TPE1``  (Artist)
    - ``TPE2``  (Album Artist) — identical to Artist
    - ``TIT2``  (Title)
    - ``TALB``  (Album) — optional
    - ``TYER``  (Year) — optional
    - ``TRSN``  (Internet Radio Station Name) — from provenance
    - ``TPUB``  (Publisher/Label) — radio station name for Jellyfin
    - ``COMM``  (Recorded via Radio-Ripper)
    - ``TXXX:RIPPEDBY`` (station@playlist) — provenance
    - ``APIC``  (Cover art, JPEG or PNG only, scaled 500–1000 px) — optional
"""

from __future__ import annotations

import contextlib
import io
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
    TPE2,
    TPUB,
    TRSN,
    TXXX,
    ID3NoHeaderError,
)

from radio_ripper.domain.models import EnrichedInfo, TrackInfo
from radio_ripper.infra.errors import TaggingError

_MIN_COVER_PX = 500
_MAX_COVER_PX = 1000


def _guess_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff") or b"JFIF" in data[:20]:
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


def _scale_cover(data: bytes) -> tuple[bytes, str] | None:
    """Scale *data* to the 500–1000 px target range and return ``(bytes, mime)``.

    Only ``image/jpeg`` and ``image/png`` are accepted; any other format
    (e.g. GIF) returns ``None`` so the cover is silently skipped.
    On Pillow import error or decode failure the original bytes are returned
    unchanged so the cover is still embedded without scaling.
    """
    mime = _guess_image_mime(data)
    if mime not in ("image/jpeg", "image/png"):
        return None
    try:
        from PIL import Image  # type: ignore[import-untyped]

        img = Image.open(io.BytesIO(data))
        w, h = img.size
        long_side = max(w, h)
        if long_side < _MIN_COVER_PX:
            scale = _MIN_COVER_PX / long_side
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
            w, h = img.size
            long_side = max(w, h)
        if long_side > _MAX_COVER_PX:
            scale = _MAX_COVER_PX / long_side
            img = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        out = io.BytesIO()
        if mime == "image/jpeg":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=90)
        else:
            img.save(out, format="PNG")
        return out.getvalue(), mime
    except ImportError:
        return data, mime
    except Exception:
        with contextlib.suppress(Exception):
            pass
        return data, mime


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
        *,
        fallback_cover: bytes | None = None,
    ) -> None:
        """Write enriched tags including album/year/genre and cover art."""

    @abstractmethod
    def update_acoustid(
        self, file_path: Path, recording_id: str, score: float
    ) -> None:
        """Add AcoustID/MusicBrainz tags to an already-tagged file."""


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
        audio.delall("TPE2")
        audio.delall("TIT2")
        audio.delall("TRSN")
        audio.delall("TPUB")
        audio.delall("COMM")
        audio.delall("TXXX:RIPPEDBY")
        if track.artist:
            audio.add(TPE1(encoding=3, text=track.artist))
            audio.add(TPE2(encoding=3, text=track.artist))
        if track.title:
            audio.add(TIT2(encoding=3, text=track.title))
        # Extract station name from provenance (format: "station@url")
        station_name = provenance.split("@")[0] if "@" in provenance else provenance
        audio.add(TRSN(encoding=3, text=station_name))
        audio.add(TPUB(encoding=3, text=station_name))
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
        *,
        fallback_cover: bytes | None = None,
    ) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path}: {exc}") from exc
        audio.delall("TPE1")
        audio.delall("TPE2")
        audio.delall("TIT2")
        audio.delall("TALB")
        audio.delall("TDRC")
        audio.delall("TRSN")
        audio.delall("TPUB")
        audio.delall("COMM")
        audio.delall("APIC")
        audio.delall("TXXX:RIPPEDBY")

        artist = enriched.artist or track.artist
        title = enriched.title or track.title
        if artist:
            audio.add(TPE1(encoding=3, text=artist))
            audio.add(TPE2(encoding=3, text=artist))
        if title:
            audio.add(TIT2(encoding=3, text=title))
        if enriched.album:
            audio.add(TALB(encoding=3, text=enriched.album))
        if enriched.year:
            audio.add(TDRC(encoding=3, text=enriched.year))
        # Extract station name from provenance (format: "station@url")
        station_name = provenance.split("@")[0] if "@" in provenance else provenance
        audio.add(TRSN(encoding=3, text=station_name))
        audio.add(TPUB(encoding=3, text=station_name))
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via Radio-Ripper"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))
        effective_cover = cover_bytes or fallback_cover
        if effective_cover:
            scaled = _scale_cover(effective_cover)
            if scaled is not None:
                scaled_data, mime = scaled
                audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=scaled_data))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save enriched tags to {file_path}: {exc}") from exc


    def update_acoustid(
        self, file_path: Path, recording_id: str, score: float
    ) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path} for acoustid tag: {exc}") from exc
        audio.delall("TXXX:MusicBrainz Recording Id")
        audio.delall("TXXX:AcoustID Score")
        if recording_id:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text=recording_id))
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text=str(round(score, 4))))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save acoustid tags to {file_path}: {exc}") from exc


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
        *,
        fallback_cover: bytes | None = None,
    ) -> None:
        return None

    def update_acoustid(
        self, file_path: Path, recording_id: str, score: float
    ) -> None:
        return None


__all__ = ["ID3Tagger", "NullTagger", "TrackTagger", "_scale_cover"]
