from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from radio_ripper.infra.http import AsyncHttpClient

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")
_LOGGER = logging.getLogger(__name__)

_ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
_ACOUSTID_MIN_SCORE = 0.9  # Mindest-Score fuer einen gueltigen Match (0.0-1.0)

_SCORE_TAG_DESC = "ACOUSTID_SCORE"

# Serializes the collision-check + rename section of finalize_with_metadata so
# concurrent recorders never race on the same target file.
_FINALIZE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class AcoustidMatch:
    """Metadata of the best AcoustID recording match."""

    artist: str
    title: str
    score: float


@dataclass(frozen=True)
class AcoustidLookup:
    """Result of an AcoustID lookup.

    ``accepted`` is True when the recording should be kept, False when the
    API answered and no result reached the minimum score. ``match`` carries
    the metadata of the best accepted match (may be None if the file passed
    but no usable metadata was returned).
    """

    accepted: bool
    match: AcoustidMatch | None


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


async def acoustid_lookup(
    path: Path,
    api_key: str,
    *,
    min_score: float = _ACOUSTID_MIN_SCORE,
    http_client: AsyncHttpClient | None = None,
) -> AcoustidLookup:
    """Query AcoustID for *path* and return score + metadata of the best match.

    Steps:
    1. Run ``fpcalc`` (Chromaprint) to compute audio fingerprint + duration.
    2. Query the AcoustID lookup API (``meta=recordings``) with the fingerprint.
    3. Pick the highest-scoring result that reaches *min_score*.

    Fail-open behavior (so recordings are never silently lost):
    - ``fpcalc`` is not installed -> ``accepted=True``, no match.
    - The API call fails for any reason -> ``accepted=True``, no match.

    ``accepted=False`` is returned only when the API responds successfully and
    no result reaches *min_score*.
    """
    fpcalc = shutil.which("fpcalc")
    if fpcalc is None:
        _LOGGER.warning("fpcalc not found — skipping AcoustID check for %s", path.name)
        return AcoustidLookup(accepted=True, match=None)

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
        return AcoustidLookup(accepted=True, match=None)

    try:
        fp_data = json.loads(stdout.decode())
        fingerprint: str = fp_data["fingerprint"]
        duration: float = float(fp_data["duration"])
    except Exception as exc:
        _LOGGER.warning("fpcalc output parse error for %s: %s — skipping AcoustID check", path.name, exc)
        return AcoustidLookup(accepted=True, match=None)

    # --- 2. Query AcoustID API ------------------------------------------------
    params = f"client={api_key}&meta=recordings&duration={int(duration)}&fingerprint={fingerprint}"
    url = f"{_ACOUSTID_LOOKUP_URL}?{params}"
    try:
        if http_client is not None:
            # Use provided httpx client (async, non-blocking)
            response_text = await asyncio.wait_for(
                http_client.get_text(url, timeout=15.0),
                timeout=20.0,
            )
            api_data = json.loads(response_text)
        else:
            # Fallback to urllib (blocking, for backward compatibility)
            import urllib.request

            loop = asyncio.get_running_loop()
            response_bytes = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: urllib.request.urlopen(url, timeout=15).read()),  # noqa: S310
                timeout=20,
            )
            api_data = json.loads(response_bytes.decode())
    except Exception as exc:
        _LOGGER.warning("AcoustID API request failed for %s: %s — skipping threshold check", path.name, exc)
        return AcoustidLookup(accepted=True, match=None)

    # --- 3. Evaluate score & extract metadata ---------------------------------
    try:
        if not isinstance(api_data, dict):
            raise ValueError("unexpected AcoustID response shape")
        parsed = _parse_acoustid_response(api_data, min_score)
    except Exception as exc:
        _LOGGER.warning("AcoustID response parse error for %s: %s — skipping threshold check", path.name, exc)
        return AcoustidLookup(accepted=True, match=None)

    if parsed is None:
        _LOGGER.info("AcoustID: no qualifying match for %s — discarding", path.name)
        return AcoustidLookup(accepted=False, match=None)

    best_score, best_result = parsed
    match = _extract_match_metadata(best_result, best_score)
    _LOGGER.info(
        "AcoustID: %s accepted (score=%.2f >= %.2f%s)",
        path.name,
        best_score,
        min_score,
        f", {match.artist} - {match.title}" if match is not None else " — no metadata",
    )
    return AcoustidLookup(accepted=True, match=match)


def _parse_acoustid_response(api_data: dict[str, Any], min_score: float) -> tuple[float, dict[str, Any]] | None:
    """Return ``(best_score, best_result)`` for the highest-scoring result that
    reaches *min_score*, or None when no result qualifies. Acceptance is purely
    score-based — metadata is extracted separately."""
    best_score = -1.0
    best_result: dict[str, Any] | None = None
    for result in api_data.get("results") or []:
        try:
            score = float(result.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if score >= min_score and score > best_score:
            best_score = score
            best_result = result
    if best_result is None:
        return None
    return best_score, best_result


def _extract_match_metadata(result: dict[str, Any], score: float) -> AcoustidMatch | None:
    """Extract artist/title from the first recording of *result* that carries
    usable metadata, or None. The score applies to the whole result."""
    for recording in result.get("recordings") or []:
        artists = [a.get("name", "").strip() for a in recording.get("artists") or [] if a.get("name")]
        artist = ", ".join(a for a in artists if a)
        title = (recording.get("title") or "").strip()
        if not artist and not title:
            continue
        return AcoustidMatch(artist=artist, title=title, score=score)
    return None


def write_mp3_tags(path: Path, *, artist: str, title: str, score: float | None = None) -> bool:
    """Write artist/title as ID3 tags into the MP3 at *path*.

    When *score* is given it is stored as a ``TXXX:ACOUSTID_SCORE`` frame so
    later collision handling can read it without a re-lookup.

    Returns True on success (or when mutagen is not installed, so recordings
    are never lost), False if tagging failed.
    """
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX, ID3NoHeaderError
    except ImportError:
        _LOGGER.warning("mutagen not installed — skipping ID3 tags for %s", path.name)
        return True
    try:
        try:
            audio = ID3(path)  # type: ignore[no-untyped-call]
        except ID3NoHeaderError:
            audio = ID3()  # type: ignore[no-untyped-call]
        if artist:
            audio.add(TPE1(encoding=3, text=[artist]))  # type: ignore[no-untyped-call]
        if title:
            audio.add(TIT2(encoding=3, text=[title]))  # type: ignore[no-untyped-call]
        if score is not None:
            audio.add(TXXX(encoding=3, desc=_SCORE_TAG_DESC, text=[f"{score:.6f}"]))  # type: ignore[no-untyped-call]
        audio.save(path)  # type: ignore[no-untyped-call]
        return True
    except Exception as exc:
        _LOGGER.warning("Failed to write ID3 tags for %s: %s", path.name, exc)
        return False


def read_mp3_score(path: Path) -> float | None:
    """Read the stored ``TXXX:ACOUSTID_SCORE`` tag of *path*, or None."""
    try:
        from mutagen.id3 import ID3, TXXX

        tags = ID3(path)  # type: ignore[no-untyped-call]
        for frame in tags.values():  # type: ignore[no-untyped-call]
            if isinstance(frame, TXXX) and frame.desc == _SCORE_TAG_DESC:  # type: ignore[attr-defined]
                return float(frame.text[0])  # type: ignore[attr-defined]
    except Exception:
        return None
    return None


def split_icy_title(title: str) -> tuple[str, str]:
    """Split an ICY 'Artist - Title' string into ``(artist, title)``.

    Falls back to ``("", title)`` when no ' - ' separator is present.
    """
    clean = title.strip()
    if " - " in clean:
        artist, _, song = clean.partition(" - ")
        return artist.strip(), song.strip()
    return "", clean


def build_metadata_filename(artist: str, title: str) -> str:
    """Build a filename '<artist> - <title>.mp3' (sanitized, '' if empty)."""
    raw = f"{artist} - {title}".strip(" -")
    safe = sanitize_filename(raw)
    if not safe:
        return ""
    return safe + ".mp3"


def rename_track(path: Path, artist: str, title: str) -> Path:
    """Rename *path* to '<artist> - <title>.mp3' in the same directory.

    Returns the new path, or the original path when no (usable) rename is
    possible. An existing target is overwritten — collision handling (e.g.
    comparing AcoustID scores) must happen before calling this function.
    """
    new_name = build_metadata_filename(artist, title)
    if not new_name or new_name == path.name:
        return path
    target = path.parent / new_name
    try:
        path.rename(target)
    except OSError as exc:
        _LOGGER.warning("Failed to rename %s -> %s: %s", path, target, exc)
        return path
    return target


async def finalize_with_metadata(
    path: Path,
    api_key: str,
    *,
    artist: str,
    title: str,
    score: float,
    http_client: AsyncHttpClient | None = None,
) -> Path:
    """Write ID3 tags to *path* and rename it to '<artist> - <title>.mp3'.

    If a file with the target name already exists, the recording with the
    higher AcoustID score wins. The existing score is read from its ID3 tag
    first and only re-queried via the API when the tag is missing (legacy
    files). If the existing score cannot be determined, the existing file is
    kept (never lose data). Returns the path of the surviving file.

    *path* should be a uniquely named staging file in the target directory so
    a losing new recording can simply be deleted. Replacement uses an atomic
    ``os.replace``. The collision + rename section is serialized so concurrent
    recorders cannot race on the same target.
    """
    target_name = build_metadata_filename(artist, title)
    if not target_name or target_name == path.name:
        write_mp3_tags(path, artist=artist, title=title, score=score)
        return path

    target = path.parent / target_name
    async with _FINALIZE_LOCK:
        if target.exists():
            existing_score = read_mp3_score(target)
            if existing_score is None:
                existing = await acoustid_lookup(target, api_key, http_client=http_client)
                existing_score = existing.match.score if existing.match else None
            if existing_score is None:
                _LOGGER.warning(
                    "Kept existing %s (score unknown); discarding %s",
                    target.name,
                    path.name,
                )
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return target
            if existing_score >= score:
                _LOGGER.info(
                    "Kept existing %s (score %.2f >= %.2f); discarding %s",
                    target.name,
                    existing_score,
                    score,
                    path.name,
                )
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                return target
            _LOGGER.info(
                "Replacing %s with %s (score %.2f > %.2f)",
                target.name,
                path.name,
                score,
                existing_score,
            )

    write_mp3_tags(path, artist=artist, title=title, score=score)
    try:
        os.replace(str(path), str(target))
    except OSError as exc:
        _LOGGER.warning("Failed to move %s -> %s: %s", path, target, exc)
        return path
    return target


__all__ = [
    "AcoustidLookup",
    "AcoustidMatch",
    "TrackWriter",
    "acoustid_lookup",
    "build_metadata_filename",
    "finalize_with_metadata",
    "get_mp3_duration",
    "is_valid_mp3",
    "read_mp3_score",
    "rename_track",
    "sanitize_filename",
    "split_icy_title",
    "write_mp3_tags",
]
