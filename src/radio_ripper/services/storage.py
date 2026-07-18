"""Safe filename + file IO layer for recorded songs."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
    """Return ``name`` with illegal filesystem characters removed/normalised."""
    if name is None:
        return "unknown"
    name = name.strip()
    if not name:
        return "unknown"
    name = name.replace("\r", " ").replace("\n", " ")
    name = _ILLEGAL_FILENAME_CHARS.sub("", name)
    name = _WHITESPACE_RE.sub(" ", name).strip()
    if not name:
        return "unknown"
    if len(name) > 200:
        name = name[:200].strip()
    return name or "unknown"


def compute_file_path(
    destination: Path,
    artist: str,
    title: str,
    stream_title_clean: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Build a safe path ``{dest}/{Artist - Title}.mp3``.

    All recordings land in *destination* directly (no per-station subfolder).
    If the candidate already exists and ``overwrite`` is False, append ``(2)``,
    ``(3)``… to the base name until a free slot is found.
    """
    if artist and title:
        base = f"{sanitize_filename(artist)} - {sanitize_filename(title)}"
    else:
        base = sanitize_filename(stream_title_clean)
    candidate = destination / f"{base}.mp3"
    if not overwrite:
        i = 2
        while candidate.exists():
            candidate = destination / f"{base} ({i}).mp3"
            i += 1
    return candidate


class TrackWriter:
    """Atomic MP3 file writer: writes bytes to a ``.tmp`` then renames on close.

    Provides a synchronous ``write`` (called from the same task) and an
    atomic ``close`` (final file appears only once fully written).
    """

    def __init__(self, final_path: Path, *, min_size: int = 1024) -> None:
        self.final_path = final_path
        self.min_size = min_size
        final_path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        self._fh = self._tmp_path.open("wb")
        self._size = 0
        self._closed = False

    @property
    def size(self) -> int:
        return self._size

    def write(self, data: bytes) -> None:
        self._fh.write(data)
        self._size += len(data)

    def flush(self) -> None:
        self._fh.flush()

    def commit(self) -> bool:
        """Atomically finalize the file. Returns False if file was discarded."""
        if self._closed:
            return False
        self._closed = True
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        if self._size < self.min_size:
            self._tmp_path.unlink(missing_ok=True)
            return False
        self._tmp_path.replace(self.final_path)
        return True

    def discard(self) -> None:
        """Abandon the recording: delete the temp file."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._fh.close()
        self._tmp_path.unlink(missing_ok=True)

    def __enter__(self) -> TrackWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        if exc_val is None:
            self.commit()
        else:
            self.discard()


def remux_mp3(path: Path) -> None:
    """Post-process a recorded MP3 via pydub/ffmpeg to fix frame-alignment.

    Decodes and re-encodes the file so that any garbage bytes before the
    first valid MP3 frame (common at ICY stream cut-points) are stripped.
    Non-fatal: if pydub or ffmpeg is unavailable the original file is kept.
    """
    tmp = path.with_suffix(".remux.tmp")
    try:
        from pydub import AudioSegment  # type: ignore[import-untyped]

        audio = AudioSegment.from_file(str(path), format="mp3")
        audio.export(str(tmp), format="mp3", tags={})
        tmp.replace(path)
    except ImportError:
        pass
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def enforce_recording_limit(station_dir: Path, max_count: int) -> list[Path]:
    """Delete the oldest MP3 files in *station_dir* when the count exceeds *max_count*.

    Files are sorted by modification time (oldest first).
    Returns the list of deleted paths.
    """
    mp3_files = sorted(station_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    deleted: list[Path] = []
    while len(mp3_files) > max_count:
        oldest = mp3_files.pop(0)
        with contextlib.suppress(OSError):
            oldest.unlink(missing_ok=True)
        deleted.append(oldest)
    return deleted


__all__ = [
    "TrackWriter",
    "compute_file_path",
    "enforce_recording_limit",
    "remux_mp3",
    "sanitize_filename",
]
