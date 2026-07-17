#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radio_ripper.py - Production-grade Webradio-Ripper.

Liest eine Liste von .m3u/.pls-Playlist-URLen, laedt jeden Stream dauerhaft
im Hintergrund herunter, trennt Lieder anhand der ICY-Metadaten
(StreamTitle-Wechsel), vermeidet Duplikate ueber eine SQLite-DB, schreibt
saubere Dateinamen und taggt die MP3s per mutagen (ID3v2).

Autonom reconnect bei Abbruch mit Exponential Backoff.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import requests

try:
    from mutagen.id3 import ID3, TIT2, TPE1, COMM, TXXX
    from mutagen.id3 import ID3NoHeaderError
except Exception as mutagen_import_error:  # pragma: no cover
    print(f"FATAL: mutagen could not be imported: {mutagen_import_error}", file=sys.stderr)
    raise


# ---------------------------------------------------------------------------
# Konstanten / Hilfsfunktionen
# ---------------------------------------------------------------------------

ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")
STREAMTITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.DOTALL)

DEFAULT_CONFIG_NAME = "config.json"
DEFAULT_CONFIG_PATHS = (
    "./config.json",
    "~/.config/radio_ripper/config.json",
    "/etc/radio_ripper/config.json",
)


def sanitize_filename(name: str) -> str:
    """Bereinigt einen String so, dass er als Dateiname taugt."""
    if name is None:
        return "unknown"
    name = name.strip()
    if not name:
        return "unknown"
    name = name.replace("\r", " ").replace("\n", " ")
    name = ILLEGAL_FILENAME_CHARS.sub("", name)
    name = WHITESPACE_RE.sub(" ", name)
    name = name.strip()
    if not name:
        return "unknown"
    # Pfadlaenge begrenzen
    if len(name) > 200:
        name = name[:200].strip()
    return name


def split_artist_title(stream_title: str) -> Tuple[str, str]:
    """Trennt 'Artist - Title' in (artist, title). Fallback auf ('', title)."""
    title = stream_title.strip()
    if " - " in title:
        artist, _, song = title.partition(" - ")
        return artist.strip(), song.strip()
    if " — " in title:
        artist, _, song = title.partition(" — ")
        return artist.strip(), song.strip()
    return "", title


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class StreamConfig:
    name: str
    url: str


@dataclass
class Config:
    destination: Path
    database: Path
    streams: List[StreamConfig]
    request_timeout: int = 30
    read_chunk: int = 4096
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    user_agent: str = "Radio-Ripper/1.0"
    overwrite_existing_files: bool = False
    min_file_size_bytes: int = 1024
    log_level: str = "INFO"
    log_file: Optional[Path] = None

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        cfg_path = Path(path).expanduser()
        if not cfg_path.is_file():
            raise FileNotFoundError(f"Config not found: {cfg_path}")
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

        destination = Path(raw.get("destination", "./recordings")).expanduser()
        database = Path(raw.get("database", str(destination / "ripper.db"))).expanduser()

        streams: List[StreamConfig] = []
        for entry in raw.get("streams", []):
            name = entry.get("name") or entry.get("url", "")
            url = entry["url"]
            streams.append(StreamConfig(name=str(name), url=str(url)))

        log_file = raw.get("log_file")
        return cls(
            destination=destination,
            database=database,
            streams=streams,
            request_timeout=int(raw.get("request_timeout", 30)),
            read_chunk=int(raw.get("read_chunk", 4096)),
            reconnect_base_delay=float(raw.get("reconnect_base_delay", 1.0)),
            reconnect_max_delay=float(raw.get("reconnect_max_delay", 60.0)),
            user_agent=str(raw.get("user_agent", "Radio-Ripper/1.0")),
            overwrite_existing_files=bool(raw.get("overwrite_existing_files", False)),
            min_file_size_bytes=int(raw.get("min_file_size_bytes", 1024)),
            log_level=str(raw.get("log_level", "INFO")).upper(),
            log_file=Path(log_file).expanduser() if log_file else None,
        )


def find_config_path(arg: Optional[str]) -> Optional[str]:
    if arg:
        return arg
    for candidate in DEFAULT_CONFIG_PATHS:
        p = Path(candidate).expanduser()
        if p.is_file():
            return str(p)
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> logging.Logger:
    logger = logging.getLogger("radio_ripper")
    logger.setLevel(getattr(logging, level, logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Playlist-Parser (m3u / pls)
# ---------------------------------------------------------------------------

def _parse_m3u(text: str) -> List[str]:
    urls: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            urls.append(line)
    return urls


def _parse_pls(text: str) -> List[str]:
    urls: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            _, _, value = line.partition("=")
            value = value.strip()
            if "://" in value:
                urls.append(value)
    return urls


def fetch_playlist_urls(url: str, session: requests.Session, logger: logging.Logger) -> List[str]:
    """Laedt eine .m3u/.pls-URL und liefert die Stream-URLs zurueck."""
    logger.info("Fetching playlist: %s", url)
    try:
        resp = session.get(url, timeout=30, headers={"User-Agent": "Radio-Ripper/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to fetch playlist %s: %s", url, exc)
        return []

    text = resp.text
    lower_url = url.lower()
    if lower_url.endswith(".pls") or "File" in text[:200]:
        return _parse_pls(text)
    return _parse_m3u(text)


# ---------------------------------------------------------------------------
# Duplikats-Datenbank (SQLite, thread-safe, pro-Stream)
# ---------------------------------------------------------------------------

class DupDB:
    """Schmale SQLite-Wrapper. Ein gemeinsames Connection-Objekt, gesichert
    durch ein threading.Lock, weil SQLite-sqlite3 Threadsafety limitiert."""

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        self.path = path
        self.logger = logger
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS songs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    station_name    TEXT NOT NULL,
                    stream_title    TEXT NOT NULL,
                    artist          TEXT,
                    title           TEXT,
                    file_path       TEXT,
                    file_size       INTEGER,
                    created_at      TEXT DEFAULT (datetime('now')),
                    UNIQUE(station_name, stream_title)
                )
                """
            )

    def exists(self, station_name: str, stream_title: str) -> bool:
        key = f"{station_name.strip()}|{stream_title.strip()}".casefold()
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM songs WHERE station_name=? AND LOWER(stream_title)=LOWER(?) LIMIT 1",
                (station_name, stream_title),
            )
            return cur.fetchone() is not None

    def register(
        self,
        station_name: str,
        stream_title: str,
        artist: str,
        title: str,
        file_path: str,
        file_size: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO songs
                    (station_name, stream_title, artist, title, file_path, file_size)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (station_name, stream_title, artist, title, file_path, file_size),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Stream-Tailing & Song-Trennung
# ---------------------------------------------------------------------------

class StreamRecorder(threading.Thread):
    """Dauerhafter Background-Thread pro Stream-URL.

    Holt die Playlist, waehlt die erste verfuegbare URL, verbindet sich mit
    Icy-MetaData:1, liest die Audio-bytes sowie Metadaten und trennt Dateien
    bei StreamTitle-Wechseln.
    """

    def __init__(
        self,
        station_name: str,
        playlist_url: str,
        config: Config,
        db: DupDB,
        logger: logging.Logger,
    ) -> None:
        super().__init__(name=f"Recorder-{station_name}", daemon=True)
        self.station_name = station_name
        self.playlist_url = playlist_url
        self.config = config
        self.db = db
        self.logger = logger
        self._stop_event = threading.Event()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": config.user_agent})
        self._session.verify = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        self.logger.info("Starting recorder '%s' for playlist '%s'", self.station_name, self.playlist_url)
        delay = self.config.reconnect_base_delay
        while not self._stop_event.is_set():
            try:
                ok = self._run_once()
            except Exception:
                self.logger.exception("Uncaught error in recorder '%s'", self.station_name)
                ok = False

            if self._stop_event.is_set():
                break

            if ok:
                # sauber geschlossen -> kurze Pause, dann wieder ran
                delay = self.config.reconnect_base_delay
            else:
                # Backoff
                self.logger.info(
                    "[%s] Reconnect in %.1fs (max %.1fs)",
                    self.station_name, delay, self.config.reconnect_max_delay,
                )
                self._stop_event.wait(delay)
                delay = min(delay * 2.0, self.config.reconnect_max_delay)

        self.logger.info("Recorder '%s' stopped.", self.station_name)

    # ------------------------------------------------------------------
    # Core-Lese-Loop
    # ------------------------------------------------------------------

    def _run_once(self) -> bool:
        urls = fetch_playlist_urls(self.playlist_url, self._session, self.logger)
        if not urls:
            self.logger.error("[%s] Playlist contained no stream URLs.", self.station_name)
            return False

        stream_url = urls[0]
        self.logger.info("[%s] Using stream URL: %s", self.station_name, stream_url)

        headers = {"Icy-MetaData": "1"}

        try:
            resp = self._session.get(
                stream_url,
                stream=True,
                timeout=(self.config.request_timeout, None),
                headers=headers,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            self.logger.error("[%s] Request failed: %s", self.station_name, exc)
            return False

        metaint = self._parse_metaint(resp)
        close_resp = None
        try:
            if metaint and metaint > 0:
                self.logger.info("[%s] icy-metaint=%d", self.station_name, metaint)
                return self._stream_with_meta(resp, metaint)
            else:
                self.logger.info("[%s] No icy-metaint header; single-file tailing mode.", self.station_name)
                return self._stream_no_meta(resp)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_metaint(resp: requests.Response) -> Optional[int]:
        for key in ("icy-metaint", "Icy-Metaint", "ICY-METAINT"):
            val = resp.headers.get(key)
            if val:
                try:
                    return int(val)
                except ValueError:
                    return None
        return None

    # ------------------------------------------------------------------
    # Variante mit Metadaten
    # ------------------------------------------------------------------

    def _stream_with_meta(self, resp: requests.Response, metaint: int) -> bool:
        audio_buf = bytearray()
        meta_len_bytes = 1
        bytes_until_meta = metaint

        current_title: Optional[str] = None
        current_file: Optional[Path] = None
        current_fh = None
        current_size = 0
        current_discarded = False  # True, wenn Duplikat: bytes mid-fly verwerfen
        max_meta_len = 16 * 255  # 4080 bytes max.

        # Erste StreamTitle zum Start benoetigen wir; bis dahin puffern wir bytes
        # ohne sie auf die Platte zu schreiben, damit der erste Song sauber startet.
        buffered_until_first_title = True

        def _safe_close_current() -> None:
            nonlocal current_fh, current_file, current_size, current_title, current_discarded
            if current_fh is not None:
                try:
                    current_fh.flush()
                    current_fh.close()
                except Exception:
                    pass
            # Wenn verworfen (Duplikat): Datei loeschen, falls erstellt
            if current_discarded and current_file is not None and current_file.exists():
                try:
                    current_file.unlink()
                except OSError:
                    pass
            elif current_file is not None and current_file.exists():
                # Zu kleine Dateien entsorgen
                if current_file.stat().st_size < self.config.min_file_size_bytes:
                    try:
                        current_file.unlink()
                        self.logger.info("[%s] Discarded (too small): %s", self.station_name, current_file.name)
                    except OSError:
                        pass
                else:
                    self._finalize(current_file, current_title or "")
            current_fh = None
            current_file = None
            current_size = 0
            current_title = None
            current_discarded = False

        try:
            stream_iter = resp.iter_content(chunk_size=self.config.read_chunk)
            for chunk in stream_iter:
                if self._stop_event.is_set():
                    self.logger.info("[%s] Stop requested; closing current file.", self.station_name)
                    _safe_close_current()
                    return True

                if not chunk:
                    continue

                audio_buf.extend(chunk)
                pos = 0
                while pos < len(audio_buf):
                    remaining_audio = len(audio_buf) - pos
                    if bytes_until_meta > 0 and remaining_audio > 0:
                        take = min(bytes_until_meta, remaining_audio)
                        audio_data = audio_buf[pos:pos + take]
                        pos += take
                        bytes_until_meta -= take

                        # Schreiben, wenn nicht verworfen und bereits ein Titel
                        # feststeht (also Datei geoeffnet).
                        if current_fh is not None and not current_discarded:
                            current_fh.write(audio_data)
                            current_size += len(audio_data)
                        elif buffered_until_first_title and current_fh is None:
                            # wir haben noch keinen Titel -> bytes verwerfen,
                            # da wir sonst Lieder-Junk am Anfang haetten.
                            pass
                    elif bytes_until_meta == 0:
                        # 1 Byte Meta-Laenge lesen
                        if remaining_audio < meta_len_bytes:
                            break  # naechster chunk
                        meta_len = audio_buf[pos] * 16
                        pos += 1
                        if meta_len > max_meta_len:
                            # unsinnig gross -> Stream korrupt
                            self.logger.warning(
                                "[%s] metadata length %d exceeds reasonable bound; reconnecting.",
                                self.station_name, meta_len,
                            )
                            _safe_close_current()
                            return False
                        if remaining_audio - 0 < meta_len:
                            # noch nicht komplett -> brauchen mehr Daten
                            pos -= 1  # meta-len-byte zuruecksetzen
                            break

                        meta_bytes = audio_buf[pos:pos + meta_len]
                        pos += meta_len
                        bytes_until_meta = metaint

                        new_title = self._parse_metadata(meta_bytes)
                        if new_title is None:
                            continue

                        if new_title != current_title:
                            # Song-Wechsel: aktuelle Datei schliessen
                            _safe_close_current()
                            current_title = new_title
                            buffered_until_first_title = False

                            stream_title_clean = new_title.strip()
                            artist, title = split_artist_title(stream_title_clean)

                            # Duplikatspruefung
                            key_for_db = stream_title_clean
                            if self.db.exists(self.station_name, key_for_db):
                                self.logger.info(
                                    "[%s] Skipping duplicate: %s", self.station_name, stream_title_clean
                                )
                                current_discarded = True
                                continue

                            if not stream_title_clean:
                                self.logger.debug("[%s] empty StreamTitle -> skip", self.station_name)
                                current_discarded = True
                                continue

                            file_path = self._make_file_path(artist, title, stream_title_clean)
                            if file_path.exists() and not self.config.overwrite_existing_files:
                                # Lokale Datei existiert bereits ohne DB-Eintrag.
                                self.logger.info(
                                    "[%s] File already exists (no db record), registering and skipping: %s",
                                    self.station_name, file_path.name,
                                )
                                self.db.register(
                                    self.station_name, stream_title_clean,
                                    artist, title, str(file_path), file_path.stat().st_size,
                                )
                                current_discarded = True
                                continue

                            try:
                                file_path.parent.mkdir(parents=True, exist_ok=True)
                                current_fh = open(file_path, "wb")
                                current_file = file_path
                                current_size = 0
                                current_discarded = False
                                self.logger.info(
                                    "[%s] Recording -> %s", self.station_name, file_path.name
                                )
                            except OSError as exc:
                                self.logger.error(
                                    "[%s] cannot open %s: %s",
                                    self.station_name, file_path, exc,
                                )
                                current_discarded = True
                                current_fh = None
                                current_file = None
                                # weiterleben, im naechsten Wechsel versuchen wir es nochmal
                    else:
                        # sollte nicht passieren
                        break

                # verarbeitete bytes abschneiden (Rest bleibt im buffer)
                if pos > 0:
                    del audio_buf[:pos]
                pos = 0

        except requests.RequestException as exc:
            self.logger.warning("[%s] stream interrupted: %s", self.station_name, exc)
            _safe_close_current()
            return False
        except Exception:
            self.logger.exception("[%s] unexpected stream error", self.station_name)
            _safe_close_current()
            return False

        # Stream zuende (EOF) - sauber abschliessen
        self.logger.info("[%s] stream ended (EOF).", self.station_name)
        _safe_close_current()
        return True

    # ------------------------------------------------------------------
    # Variante ohne Metadaten (Fallback)
    # ------------------------------------------------------------------

    def _stream_no_meta(self, resp: requests.Response) -> bool:
        """Ohne Metadaten koennen wir nicht trennen - wir schreiben einen
        fortlaufenden Dump pro Station. Genannt nach Station und Startzeit."""
        target_dir = self.config.destination / self.station_name
        target_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{sanitize_filename(self.station_name)}_{time.strftime('%Y%m%d_%H%M%S')}.mp3"
        target = target_dir / fname
        size = 0
        try:
            with open(target, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=self.config.read_chunk):
                    if self._stop_event.is_set():
                        self.logger.info("[%s] stop requested.", self.station_name)
                        return True
                    if not chunk:
                        continue
                    fh.write(chunk)
                    size += len(chunk)
        except requests.RequestException as exc:
            self.logger.warning("[%s] stream interrupted (no-meta): %s", self.station_name, exc)
            return False
        self.logger.info("[%s] stream session closed: %s (%d bytes)", self.station_name, target.name, size)
        return True

    # ------------------------------------------------------------------
    # Metadata-Parsing
    # ------------------------------------------------------------------

    def _parse_metadata(self, meta_bytes: bytes) -> Optional[str]:
        if not meta_bytes:
            return None
        try:
            text = meta_bytes.rstrip(b"\x00 ").decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return None
        m = STREAMTITLE_RE.search(text)
        if not m:
            return None
        title = m.group(1)
        title = title.replace("\\'", "'").replace("\\\\", "\\")
        return title.strip()

    # ------------------------------------------------------------------
    # Dateipfad-Erzeugung & Tagging
    # ------------------------------------------------------------------

    def _make_file_path(self, artist: str, title: str, stream_title_clean: str) -> Path:
        station_dir = self.config.destination / self.station_name
        if artist and title:
            base = f"{sanitize_filename(artist)} - {sanitize_filename(title)}"
        else:
            base = sanitize_filename(stream_title_clean)
        candidate = station_dir / f"{base}.mp3"
        if not self.config.overwrite_existing_files:
            i = 2
            while candidate.exists():
                candidate = station_dir / f"{base} ({i}).mp3"
                i += 1
        return candidate

    def _finalize(self, file_path: Path, stream_title: str) -> None:
        """Schreibt ID3v2-Tags und traegt den Song in die DB ein."""
        artist, title = split_artist_title(stream_title)
        try:
            self._write_id3(file_path, artist, title)
        except Exception as exc:
            self.logger.warning("[%s] failed to tag %s: %s", self.station_name, file_path.name, exc)
        try:
            self.db.register(
                self.station_name, stream_title.strip(), artist, title,
                str(file_path), file_path.stat().st_size,
            )
        except Exception as exc:
            self.logger.warning("[%s] failed to register %s in db: %s",
                                self.station_name, file_path.name, exc)
        self.logger.info("[%s] Completed: %s (%s bytes)",
                         self.station_name, file_path.name, file_path.stat().st_size)

    def _write_id3(self, file_path: Path, artist: str, title: str) -> None:
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.delall("TPE1")
        audio.delall("TIT2")
        audio.delall("COMM")
        audio.delall("TXXX:RIPPEDBY")
        if artist:
            audio.add(TPE1(encoding=3, text=artist))
        if title:
            audio.add(TIT2(encoding=3, text=title))
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via Radio-Ripper"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=f"{self.station_name}@{self.playlist_url}"))
        audio.save(file_path, v2_version=3, v1=2)


# ---------------------------------------------------------------------------
# Main coordinator
# ---------------------------------------------------------------------------

class RadioRipper:
    def __init__(self, config: Config, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.config.destination.mkdir(parents=True, exist_ok=True)
        self.db = DupDB(self.config.database, self.logger)
        self.recorders: List[StreamRecorder] = []

    def start(self) -> None:
        if not self.config.streams:
            self.logger.error("No streams configured. Exiting.")
            return
        for stream in self.config.streams:
            rec = StreamRecorder(
                station_name=stream.name,
                playlist_url=stream.url,
                config=self.config,
                db=self.db,
                logger=self.logger,
            )
            rec.start()
            self.recorders.append(rec)

    def stop(self) -> None:
        self.logger.info("Stopping all recorders...")
        for rec in self.recorders:
            rec.stop()
        for rec in self.recorders:
            if rec.is_alive():
                rec.join(timeout=10.0)
        self.db.close()
        self.logger.info("All recorders stopped.")


# ---------------------------------------------------------------------------
# Entry-Point
# ---------------------------------------------------------------------------

__version__ = "1.0.0"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radio-ripper",
        description=(
            "Production-grade Webradio-Ripper: dauerhaftes paralleles Aufzeichnen "
            "von ICY-Metadaten-Streams mit automatischer Song-Trennung, "
            "Duplikats-Erkennung (SQLite) und ID3v2-Tagging (mutagen)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiel:\n"
            "  uv run radio-ripper --config config.json\n"
            "  uv run radio-ripper --log-level DEBUG\n"
            "  uv run radio-ripper -c ~/.config/radio_ripper/config.json\n"
            "\n"
            "Konfiguration siehe ./config.json. Stop mit Strg+C."
        ),
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Pfad zur config.json (default: ./config.json, "
             " alternativ ~/.config/radio_ripper/config.json, /etc/radio_ripper/config.json).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Ueberschreibt log_level aus der config.json.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv if argv is not None else sys.argv[1:])

    cfg_path = args.config or find_config_path(None)
    if cfg_path is None or not Path(cfg_path).expanduser().is_file():
        print("No config found. Use --config PATH or create ./config.json", file=sys.stderr)
        return 2

    try:
        config = Config.load(cfg_path)
    except Exception as exc:
        print(f"Failed to load config '{cfg_path}': {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        config.log_level = args.log_level

    logger = setup_logging(config.log_level, config.log_file)
    logger.info("=== Radio-Ripper %s starting up ===", __version__)
    logger.info("Config file : %s", cfg_path)
    logger.info("Destination : %s", config.destination)
    logger.info("Database    : %s", config.database)
    logger.info("Streams     : %d", len(config.streams))
    if __debug__:
        logger.warning("Python laeuft ohne -O Flag (asserts aktiv). Fuer Produktion ggf. -O nutzen.")

    ripper = RadioRipper(config, logger)

    # Graceful shutdown via SIGTERM / SIGINT
    shutdown_event = threading.Event()

    def _signal_handler(signum, _frame):
        logger.info("Signal %s received - initiating graceful shutdown...", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    ripper.start()

    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received - shutting down...")
    finally:
        ripper.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())