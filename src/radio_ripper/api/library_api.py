"""Library API — browse and search recorded songs.

Reads directly from the SQLite database (read-only) and resolves
``file_path`` entries to absolute paths on disk for MP3 playback.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from radio_ripper.infra.config import Settings

__all__ = ["LibraryApi", "SongInfo"]


@dataclass(frozen=True, slots=True)
class SongInfo:
    """One row from the ``songs`` table, enriched with resolved file path."""

    id: int
    station_name: str
    stream_title: str
    artist: str
    title: str
    album: str | None
    year: str | None
    file_path: str
    file_size: int
    has_cover: bool
    created_at: str
    absolute_path: str | None


class LibraryApi:
    """Browse and search the recorded-song library."""

    def __init__(self, settings: Settings) -> None:
        self._db_path = Path(settings.database)
        self._destination = Path(settings.destination)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection) -> bool:
        """Return True if the ``songs`` table exists in the database."""
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='songs'")
        return cur.fetchone() is not None

    def list_songs(self, limit: int = 500) -> list[SongInfo]:
        """Return all songs, newest first (up to *limit*).

        Returns an empty list if the ``songs`` table does not exist yet
        (e.g. the ripper has never been started).
        """
        with self._connect() as conn:
            if not self._table_exists(conn):
                return []
            rows = conn.execute(
                "SELECT id, station_name, stream_title, artist, title, "
                "       album, year, file_path, file_size, has_cover, created_at "
                "FROM songs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_song(r) for r in rows]

    def search_songs(self, query: str, limit: int = 500) -> list[SongInfo]:
        """Full-text search across artist, title, station_name, stream_title."""
        pattern = f"%{query}%"
        with self._connect() as conn:
            if not self._table_exists(conn):
                return []
            rows = conn.execute(
                "SELECT id, station_name, stream_title, artist, title, "
                "       album, year, file_path, file_size, has_cover, created_at "
                "FROM songs "
                "WHERE artist LIKE ? OR title LIKE ? "
                "  OR station_name LIKE ? OR stream_title LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (pattern, pattern, pattern, pattern, limit),
            ).fetchall()
        return [self._row_to_song(r) for r in rows]

    def get_song(self, song_id: int) -> SongInfo | None:
        """Return a single song by its database ID, or ``None`` if not found."""
        with self._connect() as conn:
            if not self._table_exists(conn):
                return None
            row = conn.execute(
                "SELECT id, station_name, stream_title, artist, title, "
                "       album, year, file_path, file_size, has_cover, created_at "
                "FROM songs WHERE id = ?",
                (song_id,),
            ).fetchone()
        return self._row_to_song(row) if row else None

    def delete_song(self, song_id: int) -> bool:
        """Delete a song from the DB **and** remove the MP3 file from disk."""
        song = self.get_song(song_id)
        if song is None:
            return False
        with self._connect() as conn:
            if not self._table_exists(conn):
                return False
            conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        if song.absolute_path:
            p = Path(song.absolute_path)
            if p.is_file():
                p.unlink(missing_ok=True)
        return True

    def _row_to_song(self, row: sqlite3.Row) -> SongInfo:
        file_path = str(row["file_path"] or "")
        abs_path = self._resolve_path(file_path)
        return SongInfo(
            id=row["id"],
            station_name=row["station_name"],
            stream_title=row["stream_title"],
            artist=row["artist"] or "",
            title=row["title"] or "",
            album=row["album"],
            year=row["year"],
            file_path=file_path,
            file_size=row["file_size"] or 0,
            has_cover=bool(row["has_cover"]),
            created_at=row["created_at"] or "",
            absolute_path=abs_path,
        )

    def _resolve_path(self, file_path: str) -> str | None:
        """Resolve a stored ``file_path`` to an absolute filesystem path."""
        if not file_path:
            return None
        p = Path(file_path)
        if p.is_absolute():
            return str(p) if p.is_file() else None
        # Try relative to destination
        candidate = self._destination / p
        if candidate.is_file():
            return str(candidate.resolve())
        # Try as-is (cwd-relative)
        if p.is_file():
            return str(p.resolve())
        return None
