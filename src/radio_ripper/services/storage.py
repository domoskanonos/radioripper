from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import tempfile
from pathlib import Path

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
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


class _WriterState:
    OPEN = "open"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class TrackWriter:
    def __init__(self, final_path: Path, *, min_size: int = 1024) -> None:
        self.final_path = final_path
        self.min_size = min_size
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            suffix=".mp3.tmp",
            prefix="radio-ripper-",
            delete=False,
        )
        self._tmp_path = Path(tmp.name)
        self._fh = tmp
        self._size = 0
        self._state = _WriterState.OPEN

    @property
    def size(self) -> int:
        return self._size

    @property
    def state(self) -> str:
        return self._state

    def write(self, data: bytes) -> None:
        self._fh.write(data)
        self._size += len(data)

    def flush(self) -> None:
        self._fh.flush()

    def commit(self) -> bool:
        if self._state != _WriterState.OPEN:
            return False
        self._state = _WriterState.COMMITTED
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        if self._size < self.min_size:
            self._tmp_path.unlink(missing_ok=True)
            return False
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self._tmp_path), str(self.final_path))
        return True

    def discard(self) -> None:
        if self._state != _WriterState.OPEN:
            return
        self._state = _WriterState.DISCARDED
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


async def get_mp3_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        val = stdout.decode().strip()
        if not val:
            return None
        return float(val)
    except Exception:
        return None


__all__ = [
    "TrackWriter",
    "get_mp3_duration",
    "sanitize_filename",
]
