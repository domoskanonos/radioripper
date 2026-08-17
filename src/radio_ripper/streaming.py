"""streaming.py — Standalone Streaming/Recording Modul für radio-ripper.

Diese Datei enthält die gesamte Streaming-Logik in einer einzigen Datei:
Stationen laden (custom.m3u), Playlist-Auflösung, ICY-Metadaten-Parsing,
Aufnahme in .part-Dateien und Validierung (Größe + Dauer).

Nur 2 Validierungs-Tests werden gemacht:
  1. Datei ist groß genug (min_file_size_bytes)
  2. Track ist länger als min_file_duration_s (Default: 90 s)

Fertige Aufnahmen landen in work_dir/recordings/.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
from pydantic import BaseModel, Field, HttpUrl, field_validator

_LOGGER = logging.getLogger("radio_ripper.streaming")


def configure_logging(level: str, log_file: Path | None = None) -> None:
    """Konfiguriert Konsolen- und optionale Datei-Logausgabe."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _strip_jsonc_comments(text: str) -> str:
    """Entfernt JSONC-Kommentare, bewahrt Kommentar-Marker in Strings."""
    result: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        char = text[i]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            if newline == -1:
                break
            result.append("\n")
            i = newline + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminierter JSONC-Blockkommentar")
            i = end + 2
        else:
            result.append(char)
            i += 1
    return "".join(result)


class Settings(BaseModel):
    """Minimale Konfiguration für das Streaming-Modul."""

    model_config = {"extra": "ignore"}

    work_dir: Path = Field(default=Path("./work"))
    destination: Path = Field(default=Path("./destination"))
    log_level: str = "INFO"

    # Stream
    max_concurrent_streams: int = Field(default=500, ge=1)
    user_agent: str = "VLC/3.0.18 LibVLC/3.0.18"
    request_timeout: float = Field(default=30.0, ge=1.0)
    reconnect_base_delay: float = Field(default=1.0, ge=0.1)
    reconnect_max_delay: float = Field(default=60.0, ge=1.0)
    no_icy_disable_after: int = Field(default=10, ge=1)
    ignore_title_patterns: list[str] = Field(default_factory=list)

    # Die 2 Validierungs-Tests
    min_file_size_bytes: int = Field(default=1_572_864, ge=0)  # 1.5 MB
    min_file_duration_s: float = Field(default=90.0, ge=0)  # 90 Sekunden

    # AcoustID (Fingerprinting + Tagging)
    acoustid_api_key: str = ""
    acoustid_min_score: float = Field(default=0.9, ge=0.0, le=1.0)

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"ungültiges log_level: {v}")
        return v

    @field_validator("work_dir", "destination")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


def load_settings(path: str | Path | None = None) -> Settings:
    """Lädt Settings aus einer JSONC-Datei oder nutzt Defaults."""
    if path is None:
        return Settings()
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config nicht gefunden: {cfg_path}")
    text = cfg_path.read_text(encoding="utf-8")
    raw = json.loads(_strip_jsonc_comments(text))
    return Settings.model_validate(raw)


# ---------------------------------------------------------------------------
# Data Classes & M3U Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M3uEntry:
    """Ein geparster M3U-Eintrag."""

    name: str
    url: str
    source: str = ""


@dataclass(frozen=True)
class AcoustidMatch:
    """Ergebnis eines AcoustID-Lookups."""

    artist: str
    title: str
    album: str = ""
    track_number: int | None = None
    year: int | None = None
    score: float = 0.0
    confirmations: int = 0
    recording_id: str = ""
    releasegroup_id: str = ""


class StreamConfig(BaseModel):
    """Konfiguration eines einzelnen Streams."""

    name: str = Field(min_length=1, max_length=64)
    url: HttpUrl


def parse_m3u_entries(text: str, source: str = "") -> list[M3uEntry]:
    """Parst M3U-Text in strukturierte Einträge."""
    entries: list[M3uEntry] = []
    current_name = ""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):
            after_comma = line.split(",", 1)
            current_name = after_comma[1].strip() if len(after_comma) > 1 else ""
            continue

        if line.startswith("#"):
            continue

        if current_name and "://" in line:
            entries.append(M3uEntry(name=current_name, url=line, source=source))
            current_name = ""

    return entries


async def resolve_playlist(
    client: "HttpxClient",
    playlist_url: str,
    *,
    timeout: float = 30.0,
) -> list[str]:
    """Löst eine Playlist-URL in eine Liste von Stream-URLs auf.

    Ist die URL keine M3U-Playlist, wird sie direkt als Stream-URL
    zurückgegeben.
    """
    if not playlist_url.lower().endswith((".m3u", ".m3u8")):
        return [playlist_url]
    text = await client.get_text(playlist_url, timeout=timeout)
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "://" in line:
            urls.append(line)
    return urls


# ---------------------------------------------------------------------------
# ICY Parser
# ---------------------------------------------------------------------------

_STREAMTITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.DOTALL)


class IcyEvent: ...


class AudioChunk(IcyEvent):
    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __repr__(self) -> str:
        return f"AudioChunk(len={len(self.data)})"


class TitleChanged(IcyEvent):
    __slots__ = ("title",)

    def __init__(self, title: str) -> None:
        self.title = title

    def __repr__(self) -> str:
        return f"TitleChanged(title={self.title!r})"


class _State:
    WAIT_AUDIO = "WAIT_AUDIO"
    READ_META_LEN = "READ_META_LEN"
    READ_META = "READ_META"


class IcyParser:
    """Parst ICY-Metadaten aus einem MP3-Stream mit icy-metaint."""

    def __init__(self, metaint: int, *, max_meta_len: int = 16 * 255) -> None:
        if metaint <= 0:
            raise ValueError(f"metaint muss positiv sein, erhalten: {metaint}")
        self.metaint = metaint
        self.max_meta_len = max_meta_len
        self._state = _State.WAIT_AUDIO
        self._buffer = bytearray()
        self._bytes_until_meta = metaint
        self._meta_len_remaining = 0
        self._pending_events: list[IcyEvent] = []

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def events(self) -> list[IcyEvent]:
        """Verarbeitet den Puffer und gibt alle anstehenden Events zurück."""
        while True:
            produced = self._step()
            if not produced:
                break
        events = self._pending_events
        self._pending_events = []
        return events

    def _step(self) -> bool:
        if not self._buffer:
            return False

        if self._state == _State.WAIT_AUDIO:
            if self._bytes_until_meta > 0:
                take = min(self._bytes_until_meta, len(self._buffer))
                if take <= 0:
                    return False
                data = bytes(self._buffer[:take])
                del self._buffer[:take]
                self._bytes_until_meta -= take
                self._pending_events.append(AudioChunk(data))
                return True
            self._state = _State.READ_META_LEN
            return True

        if self._state == _State.READ_META_LEN:
            if len(self._buffer) < 1:
                return False
            meta_len = self._buffer[0] * 16
            del self._buffer[:1]
            if meta_len > self.max_meta_len:
                raise ValueError(f"Metadaten-Länge {meta_len} übersteigt Limit {self.max_meta_len}")
            self._meta_len_remaining = meta_len
            self._state = _State.READ_META
            return True

        if self._state == _State.READ_META:
            if len(self._buffer) < self._meta_len_remaining:
                return False
            meta_bytes = bytes(self._buffer[: self._meta_len_remaining])
            del self._buffer[: self._meta_len_remaining]
            self._meta_len_remaining = 0
            self._bytes_until_meta = self.metaint
            self._state = _State.WAIT_AUDIO
            title = _parse_stream_title(meta_bytes)
            if title is not None:
                self._pending_events.append(TitleChanged(title))
            return True

        return False


def _parse_stream_title(meta_bytes: bytes) -> str | None:
    if not meta_bytes:
        return None
    text = meta_bytes.rstrip(b"\x00 ").decode("utf-8", errors="replace")
    m = _STREAMTITLE_RE.search(text)
    if m is None:
        return None
    title = m.group(1).replace("\\'", "'").replace("\\\\", "\\").strip()
    return title if title else ""


# ---------------------------------------------------------------------------
# TrackWriter
# ---------------------------------------------------------------------------

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
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


# ---------------------------------------------------------------------------
# HTTP Client (vereinfacht)
# ---------------------------------------------------------------------------


class HttpxClient:
    """Vereinfachter async HTTP-Client für Streaming und Playlists."""

    def __init__(
        self,
        *,
        user_agent: str = "VLC/3.0.18 LibVLC/3.0.18",
        max_pool_size: int = 500,
        total_timeout: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(total_timeout, connect=10.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=max_pool_size,
                max_keepalive_connections=min(100, max_pool_size),
            ),
        )
        self._last_headers: dict[str, str] = {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text

    async def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[bytes, None]:
        async with self._client.stream("GET", url, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            self._last_headers = dict(resp.headers)
            async for chunk in resp.aiter_bytes():
                yield chunk

    def response_headers(self) -> dict[str, str]:
        return dict(self._last_headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpxClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


# ---------------------------------------------------------------------------
# Dauer-Ermittlung (Test 2) — via ThreadPool
# ---------------------------------------------------------------------------


def _ffprobe_duration_sync(path: Path) -> float | None:
    """Führt ffprobe aus (blockierend) und gibt die Dauer in Sekunden zurück."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        _LOGGER.warning("ffprobe nicht gefunden — Dauer kann nicht ermittelt werden.")
        return None

    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            timeout=10,
        )
        val = proc.stdout.decode().strip()
        if not val:
            return None
        return float(val)
    except Exception:
        return None


async def _get_duration(path: Path, executor: ThreadPoolExecutor) -> float | None:
    """Ermittelt die Datei-Dauer asynchron im ThreadPool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _ffprobe_duration_sync, path)


async def _validate_file(path: Path, settings: Settings, executor: ThreadPoolExecutor) -> bool:
    """Gibt True nur, wenn BEIDE Validierungs-Tests bestanden sind.

    Test 1: Datei ist groß genug (min_file_size_bytes).
    Test 2: Track ist länger als min_file_duration_s.
    Schlägt ein Test fehl, wird die Datei gelöscht und False zurückgegeben.
    """
    # TEST 1: Mindest-Größe
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < settings.min_file_size_bytes:
        _LOGGER.info("Zu klein (%d < %d): %s", size, settings.min_file_size_bytes, path.name)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return False

    # TEST 2: Mindest-Dauer
    if settings.min_file_duration_s > 0:
        dur = await _get_duration(path, executor)
        if dur is None:
            _LOGGER.warning("Dauer nicht bestimmbar — Datei wird behalten: %s", path.name)
        elif dur < settings.min_file_duration_s:
            _LOGGER.info(
                "Zu kurz (%.1fs < %.1fs): %s",
                dur,
                settings.min_file_duration_s,
                path.name,
            )
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return False

    return True


# ---------------------------------------------------------------------------
# AcoustID — Fingerprint, Lookup, Tagging
# ---------------------------------------------------------------------------


def _fpcalc_sync(path: Path) -> dict[str, Any] | None:
    """Führt fpcalc aus (blockierend) und liefert Fingerprint + Dauer."""
    fpcalc = shutil.which("fpcalc")
    if fpcalc is None:
        _LOGGER.warning("fpcalc nicht gefunden — AcoustID-Fingerprint nicht möglich.")
        return None
    try:
        proc = subprocess.run(
            [fpcalc, "-json", str(path)],
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        _LOGGER.warning("fpcalc fehlgeschlagen für %s: %s", path.name, exc)
        return None
    if proc.returncode:
        _LOGGER.warning("fpcalc konnte %s nicht dekodieren (exit %d)", path.name, proc.returncode)
        return None
    try:
        return json.loads(proc.stdout.decode())
    except Exception as exc:
        _LOGGER.warning("fpcalc-Ausgabe nicht lesbar für %s: %s", path.name, exc)
        return None


def acoustid_lookup(
    path: Path,
    *,
    api_key: str,
    min_score: float,
) -> tuple[AcoustidMatch | None, str]:
    """Fragt AcoustID ab.

    Gibt ``(match, status)`` zurück:
    - ``status == "ok"``      — API antwortete, ``match`` ist der beste Treffer
      (oder ``None``, wenn keiner den Mindest-Score erreicht).
    - ``status == "error"``   — API nicht erreichbar / ungültiger Key / fpcalc
      fehlgeschlagen. ``match`` ist ``None``. Die Datei soll NICHT gelöscht werden.
    """
    import urllib.parse
    import urllib.request

    fp = _fpcalc_sync(path)
    if fp is None:
        return None, "error"

    duration = int(float(fp.get("duration", 0)))

    params = urllib.parse.urlencode(
        {
            "client": api_key,
            "format": "json",
            "meta": "recordings+releasegroups",
            "duration": duration,
            "fingerprint": fp.get("fingerprint", ""),
        }
    )
    url = f"https://api.acoustid.org/v2/lookup?{params}"

    try:
        resp = urllib.request.urlopen(url, timeout=15)
        api = json.loads(resp.read().decode())
    except Exception as exc:
        _LOGGER.warning("AcoustID-API-Fehler für %s: %s", path.name, exc)
        return None, "error"
    if not isinstance(api, dict) or api.get("status") != "ok":
        _LOGGER.warning("AcoustID unerwartete Antwort für %s", path.name)
        return None, "error"

    best: AcoustidMatch | None = None

    for result in api.get("results") or []:
        try:
            score = float(result.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        for recording in result.get("recordings") or []:
            artists = [a.get("name", "").strip() for a in recording.get("artists") or [] if a.get("name")]
            artist = ", ".join(artists)
            title = (recording.get("title") or "").strip()
            if not artist or not title:
                continue
            recording_id = (recording.get("id") or "").strip()
            # Bevorzugt das erste Releasegroup als Album (single → ohne Album)
            album = ""
            year: int | None = None
            releasegroup_id = ""
            for rg in recording.get("releasegroups") or []:
                rg_title = (rg.get("title") or "").strip()
                if not album and rg_title:
                    album = rg_title
                if not releasegroup_id:
                    releasegroup_id = (rg.get("id") or "").strip()
                if year is None:
                    date = (rg.get("firstreleasedate") or "").strip()
                    if date[:4].isdigit():
                        year = int(date[:4])
            try:
                confirmations = int(recording.get("confirmations", 0) or 0)
            except (TypeError, ValueError):
                confirmations = 0
            try:
                track_number = int(recording.get("track_number") or 0) or None
            except (TypeError, ValueError):
                track_number = None

            match = AcoustidMatch(
                artist=artist,
                title=title,
                album=album,
                track_number=track_number,
                year=year,
                score=score,
                confirmations=confirmations,
                recording_id=recording_id,
                releasegroup_id=releasegroup_id,
            )
            # Besserer Score gewinnt; bei Gleichstand mehr Bestätigungen
            if best is None or (score, confirmations) > (best.score, best.confirmations):
                best = match

    if best is None:
        _LOGGER.info("AcoustID: kein Treffer ≥ %.2f für %s", min_score, path.name)
    return best, "ok"


def write_mp3_tags(
    path: Path,
    *,
    artist: str,
    title: str,
    album: str = "",
    track_number: int | None = None,
    year: int | None = None,
    score: float,
    confirmations: int = 0,
    recording_id: str = "",
    releasegroup_id: str = "",
) -> None:
    """Schreibt Artist/Title/Album/Track/Jahr/Score + MB-IDs als ID3-Tags (mutagen)."""
    try:
        from mutagen.id3 import ID3, TALB, TDRC, TIT2, TPE1, TRCK, TXXX, ID3NoHeaderError
    except ImportError:
        _LOGGER.warning("mutagen nicht installiert — Tags werden nicht geschrieben für %s", path.name)
        return
    try:
        try:
            audio = ID3(path)  # type: ignore[no-untyped-call]
        except ID3NoHeaderError:
            audio = ID3()  # type: ignore[no-untyped-call]
        if artist:
            audio.add(TPE1(encoding=3, text=[artist]))  # type: ignore[no-untyped-call]
        if title:
            audio.add(TIT2(encoding=3, text=[title]))  # type: ignore[no-untyped-call]
        if album:
            audio.add(TALB(encoding=3, text=[album]))  # type: ignore[no-untyped-call]
        if track_number:
            audio.add(TRCK(encoding=3, text=[str(track_number)]))  # type: ignore[no-untyped-call]
        if year:
            audio.add(TDRC(encoding=3, text=[str(year)]))  # type: ignore[no-untyped-call]
        audio.add(TXXX(encoding=3, desc="ACOUSTID_SCORE", text=[f"{score:.6f}"]))  # type: ignore[no-untyped-call]
        if confirmations:
            audio.add(TXXX(encoding=3, desc="ACOUSTID_CONFIRMATIONS", text=[str(confirmations)]))  # type: ignore[no-untyped-call]
        if recording_id:
            audio.add(TXXX(encoding=3, desc="MUSICBRAINZ_TRACKID", text=[recording_id]))  # type: ignore[no-untyped-call]
        if releasegroup_id:
            audio.add(TXXX(encoding=3, desc="MUSICBRAINZ_RELEASEGROUPID", text=[releasegroup_id]))  # type: ignore[no-untyped-call]
        audio.save(path)  # type: ignore[no-untyped-call]
    except Exception as exc:
        _LOGGER.warning("ID3-Tags für %s fehlgeschlagen: %s", path.name, exc)


def _build_metadata_filename(artist: str, title: str) -> str:
    """Baut ``Artist - Title.mp3`` (säuberter Dateiname)."""
    raw = f"{artist} - {title}".strip(" -")
    safe = sanitize_filename(raw)
    return safe + ".mp3" if safe else ""


def _build_target_path(destination: Path, artist: str, title: str, album: str = "") -> Path:
    """Baut den Zielpfad mit standardmäßiger MP3-Ordnerstruktur.

    Struktur: ``Artist/Album/Artist - Title.mp3``
    Fallback ohne Album: ``Artist/Artist - Title.mp3``
    """
    safe_artist = sanitize_filename(artist)
    if not safe_artist:
        safe_artist = "Unknown Artist"
    filename = _build_metadata_filename(artist, title)
    if not filename:
        return destination / safe_artist / f"{safe_artist}.mp3"

    safe_album = sanitize_filename(album)
    if safe_album:
        return destination / safe_artist / safe_album / filename
    return destination / safe_artist / filename


def _read_mp3_score(path: Path) -> float | None:
    """Liest den gespeicherten ``ACOUSTID_SCORE``-Tag einer MP3, oder None."""
    try:
        from mutagen.id3 import ID3, TXXX

        tags = ID3(path)  # type: ignore[no-untyped-call]
        for frame in tags.values():  # type: ignore[no-untyped-call]
            if isinstance(frame, TXXX) and frame.desc == "ACOUSTID_SCORE":  # type: ignore[attr-defined]
                return float(frame.text[0])  # type: ignore[attr-defined]
    except Exception:
        return None
    return None


def _move_to_destination(path: Path, target: Path) -> None:
    """Verschiebt path nach target (mit Cross-Device-Fallback)."""
    try:
        os.replace(str(path), str(target))
        return
    except OSError as exc:
        if exc.errno != 18:  # EXDEV
            raise
    import shutil as _shutil

    _shutil.copy2(path, target)
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _finalize_acoustid(final_path: Path, settings: Settings) -> None:
    """Läuft nach der Validierung: AcoustID-Lookup, Tagging, Verschieben.

    - Treffer ≥ min_score: Tags schreiben, nach destination/ verschieben
      (Ordnerstruktur Artist/Album/), bei Kollision gewinnt der höhere Score.
    - Kein Treffer: Datei löschen.
    - Fehler: Datei bleibt in recordings/ liegen.
    """
    if not settings.acoustid_api_key:
        _LOGGER.info(
            "[acoustid] Kein API-Key — Datei bleibt in recordings/: %s",
            final_path.name,
        )
        return

    match, status = acoustid_lookup(
        final_path,
        api_key=settings.acoustid_api_key,
        min_score=settings.acoustid_min_score,
    )
    if status == "error":
        _LOGGER.info(
            "[acoustid] API-Fehler — Datei bleibt in recordings/: %s",
            final_path.name,
        )
        return
    if match is None:
        _LOGGER.info("[acoustid] Kein Treffer — lösche: %s", final_path.name)
        with contextlib.suppress(OSError):
            final_path.unlink(missing_ok=True)
        return

    target = _build_target_path(
        settings.destination,
        match.artist,
        match.title,
        match.album,
    )

    # Tags zuerst schreiben, damit Kollision via ACOUSTID_SCORE vergleichbar ist
    write_mp3_tags(
        final_path,
        artist=match.artist,
        title=match.title,
        album=match.album,
        track_number=match.track_number,
        year=match.year,
        score=match.score,
        confirmations=match.confirmations,
        recording_id=match.recording_id,
        releasegroup_id=match.releasegroup_id,
    )

    # Kollision: bestehende Datei mit höherem/gleichem Score behalten
    if target.exists():
        existing_score = _read_mp3_score(target)
        if existing_score is not None and existing_score >= match.score:
            _LOGGER.info(
                "[acoustid] Kollision: %s behalten (Score %.2f >= %.2f) — verwerfe %s",
                target.name,
                existing_score,
                match.score,
                final_path.name,
            )
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            return
        if existing_score is not None:
            _LOGGER.info(
                "[acoustid] Kollision: ersetze %s (Score %.2f < %.2f)",
                target.name,
                existing_score,
                match.score,
            )
        else:
            _LOGGER.info(
                "[acoustid] Kollision: bestehende %s ohne Score — ersetze (Score %.2f)",
                target.name,
                match.score,
            )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _move_to_destination(final_path, target)
    except OSError as exc:
        _LOGGER.error("[acoustid] Verschieben fehlgeschlagen für %s: %s", final_path.name, exc)
        return

    _LOGGER.info(
        "[acoustid] Accepted: %s (score=%.2f)",
        target,
        match.score,
    )


class AcoustidWorker:
    """Singleton-Worker für AcoustID — verarbeitet fertige Aufnahmen sequenziell.

    Recorder legen fertige, validierte Dateien per ``enqueue()`` in eine Queue
    und streamen sofort weiter. Ein einzelner asyncio-Task verarbeitet die
    Dateien nacheinander (fpcalc + API + Tagging + Verschieben), was zugleich
    ein natürliches Rate-Limit für die AcoustID-API darstellt. Die blockierenden
    Aufrufe laufen via ``asyncio.to_thread`` im Default-Executor — getrennt vom
    ffprobe-ThreadPool.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def enqueue(self, path: Path) -> None:
        """Reicht eine fertige Datei zur Verarbeitung ein (blockiert nie)."""
        if self._stopped:
            return
        self._queue.put_nowait(path)

    def start(self) -> None:
        """Startet den Worker-Task (muss in einer laufenden Event-Loop sein)."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="AcoustidWorker")

    async def stop(self) -> None:
        """Stoppt den Worker nach dem Abarbeiten der Reste der Queue."""
        self._stopped = True
        self._queue.put_nowait(Path("__stop__"))
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=30.0)
            self._task = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def _run(self) -> None:
        while True:
            path = await self._queue.get()
            if path.name == "__stop__":
                break
            try:
                await asyncio.to_thread(_finalize_acoustid, path, self._settings)
            except Exception:
                _LOGGER.exception("AcoustidWorker: unerwarteter Fehler für %s", path.name)


# ---------------------------------------------------------------------------
# StreamRecorder
# ---------------------------------------------------------------------------


def cleanup_stale_parts(work_dir: Path) -> int:
    """Entfernt übrig gebliebene ``.part``-Dateien aus abgebrochenen Läufen."""
    staging = work_dir / "recordings"
    if not staging.is_dir():
        return 0
    parts = sorted(staging.glob("*.part"))
    for part in parts:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
    if parts:
        _LOGGER.info("Entfernt %d unvollständige Aufnahme(n) (.part) aus einem früheren Lauf.", len(parts))
    return len(parts)


class StreamRecorder:
    """Nimmt einen einzelnen Radiostream auf und validiert jeden Track."""

    def __init__(
        self,
        *,
        station: StreamConfig,
        settings: Settings,
        client: HttpxClient,
        executor: ThreadPoolExecutor,
        acoustid_worker: AcoustidWorker | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.station = station
        self.settings = settings
        self._client = client
        self._executor = executor
        self._acoustid_worker = acoustid_worker
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        pats = settings.ignore_title_patterns or []
        self._ignore_patterns: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in pats]
        self._no_icy_failures = 0
        self._connect_failures = 0

    @property
    def station_name(self) -> str:
        return self.station.name

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        self._stop_event.set()

    async def join(self) -> None:
        if self._task is not None:
            await self._task

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self._run_forever(), name=f"Recorder-{self.station.name}")
        return self._task

    # ------------------------------------------------------------------ core loop

    async def _run_forever(self) -> None:
        self._log.info(
            "Starte Recorder '%s' für '%s'",
            self.station.name,
            self.station.url,
        )
        delay = self.settings.reconnect_base_delay
        while not self._stop_event.is_set():
            try:
                ok = await self._run_once()
            except Exception:
                self._log.exception("Unerwarteter Fehler in Recorder '%s'", self.station.name)
                ok = False
            if self._stop_event.is_set():
                break
            if self._no_icy_failures >= self.settings.no_icy_disable_after:
                self._log.error(
                    "[%s] Deaktiviert: kein ICY-Metadaten nach %d Versuchen. "
                    "Stream unterstützt vermutlich kein ICY.",
                    self.station.name,
                    self._no_icy_failures,
                )
                break
            if self._connect_failures >= self.settings.no_icy_disable_after:
                self._log.error(
                    "[%s] Deaktiviert: %d Verbindungsfehler in Folge.",
                    self.station.name,
                    self._connect_failures,
                )
                break
            if ok:
                delay = self.settings.reconnect_base_delay
            else:
                self._log.info(
                    "[%s] Reconnect in %.1fs (max %.1fs)",
                    self.station.name,
                    delay,
                    self.settings.reconnect_max_delay,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                delay = min(delay * 2.0, self.settings.reconnect_max_delay)
                delay *= 1.0 + random.random() * 0.5  # noqa: S311  -- Jitter gegen Thundering-Herd
        self._log.info("Recorder '%s' gestoppt.", self.station.name)

    async def _run_once(self) -> bool:
        try:
            urls = await resolve_playlist(
                self._client,
                str(self.station.url),
                timeout=self.settings.request_timeout,
            )
        except Exception as exc:
            self._log.error("[%s] Playlist-Fehler: %s", self.station.name, exc)
            self._connect_failures += 1
            return False
        if not urls:
            self._log.error("[%s] Playlist enthielt keine Stream-URLs.", self.station.name)
            return False
        stream_url = urls[0]
        self._log.info("[%s] Verwende Stream-URL: %s", self.station.name, stream_url)
        try:
            ok = await self._stream_with_meta(stream_url)
            self._connect_failures = 0
            return ok
        except httpx.TimeoutException:
            self._log.error("[%s] Timeout beim Verbinden.", self.station.name)
            self._connect_failures += 1
            return False
        except httpx.HTTPError as exc:
            self._log.error("[%s] HTTP-Fehler: %s", self.station.name, exc)
            self._connect_failures += 1
            return False

    # ------------------------------------------------------------------ stream helpers

    async def _connect_stream(self, stream_url: str) -> tuple[AsyncGenerator[bytes, None], IcyParser] | None:
        headers = {"Icy-MetaData": "1"}
        agen = self._client.stream_binary(
            stream_url,
            headers=headers,
            timeout=self.settings.request_timeout,
        )
        try:
            first_chunk = await agen.__anext__()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await agen.aclose()
            self._log.warning(
                "[%s] Verbindung fehlgeschlagen: %s: %r",
                self.station.name,
                type(exc).__name__,
                exc,
            )
            raise
        resp_headers = self._client.response_headers()
        try:
            metaint = int(resp_headers.get("icy-metaint", 0))
        except (TypeError, ValueError):
            metaint = 0
        if not metaint or metaint <= 0:
            self._no_icy_failures += 1
            self._log.info(
                "[%s] Kein icy-metaint-Header; schließe. (Fehler %d/%d)",
                self.station.name,
                self._no_icy_failures,
                self.settings.no_icy_disable_after,
            )
            with contextlib.suppress(Exception):
                await agen.aclose()
            return None
        self._no_icy_failures = 0
        self._log.info("[%s] icy-metaint=%d", self.station.name, metaint)
        parser = IcyParser(metaint)
        parser.feed(first_chunk or b"")
        return agen, parser

    def _make_writer(self, icy_title: str) -> TrackWriter | None:
        """Erstellt einen TrackWriter im work_dir/recordings."""
        recordings_dir = self.settings.work_dir / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_filename(icy_title)
        if not safe_name:
            self._log.error("[%s] Kein Dateiname für Titel=%r", self.station.name, icy_title)
            return None

        file_path = recordings_dir / f"{safe_name}.mp3"
        try:
            return TrackWriter(file_path)
        except OSError as exc:
            self._log.error("[%s] Konnte %s nicht öffnen: %s", self.station.name, file_path, exc)
            return None

    def _should_record_title(self, title: str) -> bool:
        clean = title.strip()
        if not clean:
            self._log.info("[%s] Leerer Titel, übersprungen", self.station.name)
            return False
        if self._ignore_patterns and any(p.search(clean) for p in self._ignore_patterns):
            self._log.info("[%s] Ignorierter Titel (Werbung?): %s", self.station.name, clean)
            return False
        return True

    # ------------------------------------------------------------------ main stream loop

    async def _stream_with_meta(self, stream_url: str) -> bool:
        connected = await self._connect_stream(stream_url)
        if connected is None:
            return False
        agen, parser = connected

        first_title_seen: str | None = None
        current_title: str | None = None
        writer: TrackWriter | None = None
        recording = False

        try:
            async for chunk in agen:
                if self._stop_event.is_set():
                    if writer is not None:
                        writer.discard()
                    return True
                if not chunk:
                    continue
                parser.feed(chunk)
                for event in parser.events():
                    if isinstance(event, AudioChunk):
                        if recording and writer is not None:
                            writer.write(event.data)
                    elif isinstance(event, TitleChanged):
                        new_title = event.title
                        if first_title_seen is None:
                            first_title_seen = new_title
                            current_title = new_title
                            self._log.info(
                                "[%s] Mitten im Song '%s' eingestiegen — warte auf nächste Grenze.",
                                self.station.name,
                                new_title,
                            )
                            continue
                        if new_title == current_title:
                            continue
                        if recording and writer is not None:
                            await self._finalize_writer(writer)
                            writer = None
                            recording = False
                        current_title = new_title
                        if not self._should_record_title(new_title):
                            continue
                        writer = self._make_writer(new_title.strip())
                        if writer is not None:
                            recording = True
                            self._log.info(
                                "[%s] Aufnahme -> %s",
                                self.station.name,
                                writer.final_path.name,
                            )
                        else:
                            recording = False
            self._log.info("[%s] Stream beendet (EOF).", self.station.name)
            if writer is not None:
                writer.discard()
            return True
        except Exception as exc:
            self._log.warning("[%s] Stream unterbrochen: %s", self.station.name, exc)
            if writer is not None:
                writer.discard()
            return False
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    async def _finalize_writer(self, writer: TrackWriter) -> None:
        """Committet den Track und führt beide Validierungs-Tests aus."""
        committed = writer.commit()
        if not committed:
            return

        final_path = writer.final_path
        ok = await _validate_file(final_path, self.settings, self._executor)
        if not ok:
            return

        self._log.info(
            "[%s] Fertig (beide Tests bestanden): %s (%d bytes)",
            self.station.name,
            final_path.name,
            final_path.stat().st_size,
        )

        # AcoustID-Verarbeitung an den Singleton-Worker abgeben (blockiert nie)
        if self._acoustid_worker is not None:
            self._acoustid_worker.enqueue(final_path)
        else:
            self._log.info(
                "[%s] Kein AcoustID-Worker — Datei bleibt in recordings/: %s",
                self.station.name,
                final_path.name,
            )
# ---------------------------------------------------------------------------
# Stationen laden
# ---------------------------------------------------------------------------


async def load_stations(settings: Settings) -> list[StreamConfig]:
    """Lädt Stationen aus ``work_dir/stations/custom.m3u``."""
    custom_m3u = settings.work_dir / "stations" / "custom.m3u"
    if not custom_m3u.is_file():
        _LOGGER.warning("Keine custom.m3u gefunden: %s", custom_m3u)
        return []

    text = custom_m3u.read_text("utf-8")
    entries = parse_m3u_entries(text, source="custom")

    stations: list[StreamConfig] = []
    for e in entries:
        try:
            stations.append(StreamConfig(name=e.name[:64], url=e.url))
        except Exception as exc:
            _LOGGER.warning("Ungültiger Eintrag %s: %s", e.name, exc)

    _LOGGER.info("%d Stationen aus custom.m3u geladen", len(stations))
    return stations


# ---------------------------------------------------------------------------
# Recorder starten
# ---------------------------------------------------------------------------


async def _start_recorders(
    settings: Settings,
    client: HttpxClient,
    executor: ThreadPoolExecutor,
    acoustid_worker: AcoustidWorker | None = None,
) -> list[StreamRecorder]:
    """Startet einen Recorder pro aktiver Station."""
    stations = await load_stations(settings)
    stations = stations[: settings.max_concurrent_streams]
    recorders: list[StreamRecorder] = []
    for station in stations:
        rec = StreamRecorder(
            station=station,
            settings=settings,
            client=client,
            executor=executor,
            acoustid_worker=acoustid_worker,
        )
        rec.start()
        recorders.append(rec)
    _LOGGER.info("%d Recorder gestartet.", len(recorders))
    return recorders


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run_stations(settings: Settings) -> None:
    """Startet das Streaming-Modul mit den übergebenen Settings."""
    cleanup_stale_parts(settings.work_dir)

    # ThreadPool-Größe = Anzahl der Sender (nur für ffprobe Länge/Größen-Test)
    stations = await load_stations(settings)
    pool_size = max(1, min(len(stations), settings.max_concurrent_streams))
    executor = ThreadPoolExecutor(max_workers=pool_size)

    # AcoustID-Singleton-Worker (sequenziell, eigener asyncio-Task)
    acoustid_worker = AcoustidWorker(settings)
    acoustid_worker.start()

    async with HttpxClient(
        user_agent=settings.user_agent,
        max_pool_size=settings.max_concurrent_streams,
        total_timeout=settings.request_timeout,
    ) as client:
        recorders = await _start_recorders(settings, client, executor, acoustid_worker)

        stop_event = asyncio.Event()

        def _signal_handler(signum: int, _frame: object | None) -> None:
            _LOGGER.info("Signal %s empfangen — fahre herunter...", signum)
            stop_event.set()
            for rec in recorders:
                rec.stop()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _signal_handler, sig, None)

        try:
            await stop_event.wait()
        finally:
            for rec in recorders:
                rec.stop()
            await asyncio.gather(*(rec.join() for rec in recorders), return_exceptions=True)

    # AcoustID-Worker beenden (wartet auf Queue-Reste)
    await acoustid_worker.stop()
    executor.shutdown(wait=True)
    _LOGGER.info("Alle Recorder gestoppt. Tschüss!")


def main(argv: list[str] | None = None) -> int:
    """CLI-Einstiegspunkt für das Streaming-Modul."""
    parser = argparse.ArgumentParser(
        prog="radio-ripper-streaming",
        description="Standalone Streaming/Recording für radio-ripper.",
    )
    parser.add_argument("-c", "--config", default="config/config.jsonc", help="Config-Datei (JSONC)")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log-Level überschreiben",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        settings = load_settings(args.config)
    except Exception as exc:
        print(f"Konnte Config nicht laden: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})

    # AcoustID-Key aus der Umgebung (ACOUST_ID) übernehmen, falls in Config nicht gesetzt
    api_key = os.environ.get("ACOUST_ID", "").strip()
    if api_key and not settings.acoustid_api_key:
        settings = settings.model_copy(update={"acoustid_api_key": api_key})

    configure_logging(settings.log_level, settings.work_dir / "streaming.log")

    try:
        asyncio.run(run_stations(settings))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt — beendet.")
        return 0
    return 0


# ---------------------------------------------------------------------------
# Tests (nur 2: Größe + Dauer)
# ---------------------------------------------------------------------------


def _run_tests() -> int:
    """Führt die 2 Validierungs-Tests aus. Gibt Exit-Code 0 bei Erfolg zurück."""
    import tempfile
    from unittest.mock import AsyncMock, patch

    configure_logging("INFO")
    _LOGGER.info("=== Tests streaming.py ===")

    async def _test_min_file_size() -> None:
        """Test 1: Datei muss groß genug sein (min_file_size_bytes)."""
        settings = Settings(min_file_size_bytes=1_572_864)
        with tempfile.TemporaryDirectory() as td:
            too_small = Path(td) / "small.mp3"
            too_small.write_bytes(b"x" * 100)  # 100 bytes < 1.5 MB
            with patch("__main__._get_duration", new=AsyncMock(return_value=120.0)):
                assert await _validate_file(too_small, settings, ThreadPoolExecutor(1)) is False
            assert not too_small.exists(), "Zu kleine Datei wurde nicht gelöscht"

            big_enough = Path(td) / "big.mp3"
            big_enough.write_bytes(b"x" * (1_572_864 + 1))
            with patch("__main__._get_duration", new=AsyncMock(return_value=120.0)):
                ok = await _validate_file(big_enough, settings, ThreadPoolExecutor(1))
            assert ok, "Große Datei sollte den Größen-Test bestehen"
            assert big_enough.exists()
        _LOGGER.info("TEST 1 (Mindestgröße): OK")

    async def _test_min_duration() -> None:
        """Test 2: Track muss länger als min_file_duration_s sein."""
        settings = Settings(min_file_duration_s=90.0)
        with tempfile.TemporaryDirectory() as td:
            short = Path(td) / "short.mp3"
            short.write_bytes(b"x" * (1_572_864 + 1))
            with patch("__main__._get_duration", new=AsyncMock(return_value=60.0)):
                assert await _validate_file(short, settings, ThreadPoolExecutor(1)) is False
            assert not short.exists(), "Zu kurze Datei wurde nicht gelöscht"

            long_mp3 = Path(td) / "long.mp3"
            long_mp3.write_bytes(b"x" * (1_572_864 + 1))
            with patch("__main__._get_duration", new=AsyncMock(return_value=120.0)):
                assert await _validate_file(long_mp3, settings, ThreadPoolExecutor(1)) is True
            assert long_mp3.exists(), "Langer Track sollte behalten werden"
        _LOGGER.info("TEST 2 (Mindestdauer): OK")

    try:
        asyncio.run(_test_min_file_size())
        asyncio.run(_test_min_duration())
    except AssertionError as exc:
        _LOGGER.error("Test fehlgeschlagen: %s", exc)
        return 1

    _LOGGER.info("=== Alle Tests bestanden ===")
    return 0


if __name__ == "__main__":
    if os.environ.get("STREAMING_TEST"):
        sys.exit(_run_tests())
    sys.exit(main())
