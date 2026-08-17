"""writer.py — TrackWriter für atomare .part → .mp3 Aufnahmen."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path

_LOGGER = logging.getLogger("radio_ripper.writer")

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_filename(name: str | None) -> str:
    """Säubert einen Dateinamen (entfernt illegale Zeichen, begrenzt Länge)."""
    if name is None:
        return ""
    name = name.strip()
    if not name:
        return ""
    name = name.replace("\r", " ").replace("\n", " ")
    name = _ILLEGAL_FILENAME_CHARS.sub("", name)
    name = _WHITESPACE_RE.sub(" ", name).strip()
    if not name:
        return ""
    if len(name) > 200:
        name = name[:200].strip()
    return name


class TrackWriter:
    """Schreibt Audio in eine ``.part``-Datei und committet sie atomar."""

    _OPEN = "open"
    _COMMITTED = "committed"
    _DISCARDED = "discarded"

    def __init__(self, final_path: Path) -> None:
        self.final_path = final_path
        self._tmp_path = final_path.with_suffix(".part")
        self._tmp_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._tmp_path.open("xb")
        self._size = 0
        self._state = self._OPEN

    @property
    def size(self) -> int:
        return self._size

    @property
    def state(self) -> str:
        return self._state

    def write(self, data: bytes) -> None:
        self._fh.write(data)
        self._size += len(data)

    def commit(self) -> bool:
        """Schließt die Datei und benennt sie atomar zu ``.mp3`` um."""
        if self._state != self._OPEN:
            return False
        self._state = self._COMMITTED
        try:
            self._fh.flush()
            self._fh.close()
        except Exception as exc:
            _LOGGER.warning("Fehler beim Schließen von %s: %s", self._tmp_path, exc)
            with contextlib.suppress(OSError):
                self._tmp_path.unlink(missing_ok=True)
            return False
        try:
            self.final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(self._tmp_path), str(self.final_path))
        except OSError as exc:
            _LOGGER.warning("Commit fehlgeschlagen für %s: %s", self._tmp_path, exc)
            with contextlib.suppress(OSError):
                self._tmp_path.unlink(missing_ok=True)
            return False
        return True

    def discard(self) -> None:
        if self._state != self._OPEN:
            return
        self._state = self._DISCARDED
        with contextlib.suppress(Exception):
            self._fh.close()
        with contextlib.suppress(OSError):
            self._tmp_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.final_path.unlink(missing_ok=True)
