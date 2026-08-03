from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_LOGGER = logging.getLogger(__name__)

_ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
_ACOUSTID_MIN_SCORE = 0.9  # Mindest-Score fuer einen gueltigen Match (0.0-1.0)


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
        except Exception as exc:
            _LOGGER.warning("Failed to flush/close temp file %s: %s", self._tmp_path, exc)
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


async def is_valid_mp3(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(4096)
        return any(head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 for i in range(len(head) - 1))
    except OSError:
        return False


async def acoustid_meets_threshold(
    path: Path,
    api_key: str,
    *,
    min_score: float = _ACOUSTID_MIN_SCORE,
) -> bool:
    """Return True if the MP3 at *path* has an AcoustID match score >= *min_score*.

    Steps:
    1. Run ``fpcalc`` (Chromaprint) to compute audio fingerprint + duration.
    2. Query the AcoustID lookup API with the fingerprint.
    3. Accept the file if at least one result has score >= *min_score*.

    Returns True (i.e. keep the file) when:
    - ``fpcalc`` is not installed (fail-open so recordings are not silently lost).
    - The API call fails for any reason (network error, timeout, etc.).
    - A matching result is found with a sufficiently high score.

    Returns False (i.e. discard the file) only when the API responds successfully
    and every result has a score below *min_score*.
    """
    fpcalc = shutil.which("fpcalc")
    if fpcalc is None:
        _LOGGER.warning("fpcalc not found — skipping AcoustID check for %s", path.name)
        return True

    # --- 1. Compute fingerprint -----------------------------------------------
    try:
        proc = await asyncio.create_subprocess_exec(
            fpcalc,
            "-json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as exc:
        _LOGGER.warning("fpcalc failed for %s: %s — skipping AcoustID check", path.name, exc)
        return True

    try:
        fp_data = json.loads(stdout.decode())
        fingerprint: str = fp_data["fingerprint"]
        duration: float = float(fp_data["duration"])
    except Exception as exc:
        _LOGGER.warning("fpcalc output parse error for %s: %s — skipping AcoustID check", path.name, exc)
        return True

    # --- 2. Query AcoustID API ------------------------------------------------
    params = f"client={api_key}&meta=recordings&duration={int(duration)}&fingerprint={fingerprint}"
    url = f"{_ACOUSTID_LOOKUP_URL}?{params}"
    try:
        import urllib.request

        loop = asyncio.get_running_loop()
        response_bytes = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: urllib.request.urlopen(url, timeout=15).read()),  # noqa: S310
            timeout=20,
        )
        api_data = json.loads(response_bytes.decode())
    except Exception as exc:
        _LOGGER.warning("AcoustID API request failed for %s: %s — skipping threshold check", path.name, exc)
        return True

    # --- 3. Evaluate score ----------------------------------------------------
    results = api_data.get("results", [])
    if not results:
        _LOGGER.info("AcoustID: no results for %s — discarding", path.name)
        return False

    best_score: float = max((r.get("score", 0.0) for r in results), default=0.0)
    if best_score >= min_score:
        _LOGGER.info("AcoustID: %s accepted (score=%.2f >= %.2f)", path.name, best_score, min_score)
        return True

    _LOGGER.info(
        "AcoustID: %s discarded (best score=%.2f < threshold=%.2f)",
        path.name,
        best_score,
        min_score,
    )
    return False


__all__ = [
    "TrackWriter",
    "acoustid_meets_threshold",
    "get_mp3_duration",
    "is_valid_mp3",
    "sanitize_filename",
]
