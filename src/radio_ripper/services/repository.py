"""Track repository — persistence of successfully recorded songs.

The :class:`TrackRepository` ABC abstracts persistence so the SQLite backend can
be swapped (or tested with an in-memory implementation). SQLite implementation
keeps a single shared connection guarded by an :class:`asyncio.Lock` because the
default ``sqlite3`` driver is synchronous and not thread-safe by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.errors import RepositoryError

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_name    TEXT NOT NULL,
    stream_title    TEXT NOT NULL,
    artist          TEXT,
    title           TEXT,
    album           TEXT,
    year            TEXT,
    file_path       TEXT,
    file_size       INTEGER,
    has_cover       INTEGER DEFAULT 0,
    enrichment      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(station_name, stream_title)
)
"""

_MIGRATION_COLUMNS = (
    ("album", "TEXT"),
    ("year", "TEXT"),
    ("has_cover", "INTEGER DEFAULT 0"),
    ("enrichment", "TEXT"),
)


class TrackRepository(ABC):
    """Persistence port for recorded-track metadata."""

    @abstractmethod
    async def exists(self, station_name: str, stream_title: str) -> bool:
        """Return ``True`` if ``(station_name, stream_title)`` is already stored."""

    @abstractmethod
    async def register(self, track: SavedTrack, station_name: str) -> None:
        """Insert a recorded track (ignored on duplicate key)."""

    @abstractmethod
    async def update_enrichment(
        self,
        station_name: str,
        stream_title: str,
        *,
        artist: str | None = None,
        title: str | None = None,
        album: str | None = None,
        year: str | None = None,
        file_size: int | None = None,
        has_cover: bool = False,
        enrichment: str = "",
    ) -> None:
        """Update a previously-registered track with enrichment results."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources."""


class SQLiteTrackRepository(TrackRepository):
    """Default SQLite (WAL) backend, async-safe via an :class:`asyncio.Lock`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(_CREATE_SCHEMA)
        for col, decl in _MIGRATION_COLUMNS:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE songs ADD COLUMN {col} {decl}")

    @staticmethod
    def _run(coro):
        return coro

    async def exists(self, station_name: str, stream_title: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._exists_sync, station_name, stream_title)

    def _exists_sync(self, station_name: str, stream_title: str) -> bool:
        try:
            cur = self._conn.execute(
                "SELECT 1 FROM songs "
                "WHERE LOWER(station_name)=LOWER(?) AND LOWER(stream_title)=LOWER(?) LIMIT 1",
                (station_name, stream_title),
            )
            return cur.fetchone() is not None
        except sqlite3.Error as exc:
            raise RepositoryError(f"exists() failed: {exc}") from exc

    async def register(self, track: SavedTrack, station_name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._register_sync, track, station_name)

    def _register_sync(self, track: SavedTrack, station_name: str) -> None:
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO songs
                    (station_name, stream_title, artist, title,
                     album, year, file_path, file_size, has_cover, enrichment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    station_name, track.stream_title, track.artist, track.title,
                    track.album, track.year, track.file_path, track.file_size,
                    1 if track.has_cover else 0, track.enrichment,
                ),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"register() failed: {exc}") from exc

    async def update_enrichment(
        self,
        station_name: str,
        stream_title: str,
        *,
        artist: str | None = None,
        title: str | None = None,
        album: str | None = None,
        year: str | None = None,
        file_size: int | None = None,
        has_cover: bool = False,
        enrichment: str = "",
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_enrichment_sync,
                station_name, stream_title,
                artist, title, album, year, file_size,
                has_cover, enrichment,
            )

    def _update_enrichment_sync(
        self,
        station_name: str,
        stream_title: str,
        artist: str | None,
        title: str | None,
        album: str | None,
        year: str | None,
        file_size: int | None,
        has_cover: bool,
        enrichment: str,
    ) -> None:
        try:
            self._conn.execute(
                """
                UPDATE songs SET
                    artist = COALESCE(?, artist),
                    title  = COALESCE(?, title),
                    album  = COALESCE(?, album),
                    year   = COALESCE(?, year),
                    file_size = COALESCE(?, file_size),
                    has_cover = ?,
                    enrichment = ?
                WHERE station_name=? AND LOWER(stream_title)=LOWER(?)
                """,
                (
                    artist, title, album, year, file_size,
                    1 if has_cover else 0, enrichment,
                    station_name, stream_title,
                ),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"update_enrichment() failed: {exc}") from exc

    async def aclose(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)


__all__ = ["SQLiteTrackRepository", "TrackRepository"]
