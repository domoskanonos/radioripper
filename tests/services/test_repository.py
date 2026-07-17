"""Tests for radio_ripper.services.repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.errors import RepositoryError
from radio_ripper.services.repository import SQLiteTrackRepository


class TestSQLiteTrackRepository:
    async def test_register_and_exists(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="Adele - Hello", artist="Adele", title="Hello",
            file_path="/tmp/x.mp3", file_size=100,
        )
        await sqlite_repo.register(track, "TopHits")
        assert await sqlite_repo.exists("TopHits", "Adele - Hello")

    async def test_exists_case_insensitive(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="Adele - Hello", artist="Adele", title="Hello",
            file_path="/tmp/x.mp3", file_size=100,
        )
        await sqlite_repo.register(track, "TopHits")
        assert await sqlite_repo.exists("tophits", "adele - hello")

    async def test_exists_returns_false_for_unknown(self, sqlite_repo: SQLiteTrackRepository):
        assert not await sqlite_repo.exists("TopHits", "Unknown - Song")

    async def test_register_is_idempotent(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="A - B", artist="A", title="B",
            file_path="/x", file_size=1,
        )
        await sqlite_repo.register(track, "Rock")
        await sqlite_repo.register(track, "Rock")
        assert await sqlite_repo.exists("Rock", "A - B")

    async def test_different_stations_allow_same_title(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="A - B", artist="A", title="B",
            file_path="/x", file_size=1,
        )
        await sqlite_repo.register(track, "Rock")
        await sqlite_repo.register(track, "Dance")
        assert await sqlite_repo.exists("Rock", "A - B")
        assert await sqlite_repo.exists("Dance", "A - B")

    async def test_update_enrichment(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="Adele - Hello", artist="Adele", title="Hello",
            file_path="/x.mp3", file_size=100,
        )
        await sqlite_repo.register(track, "TopHits")
        await sqlite_repo.update_enrichment(
            "TopHits", "Adele - Hello",
            album="25", year="2015", file_size=200,
            has_cover=True, enrichment="itunes",
        )
        # Verify by re-registering won't change, but enrichment was updated.
        # We check exists still true
        assert await sqlite_repo.exists("TopHits", "Adele - Hello")

    async def test_update_enrichment_unknown_song_no_error(self, sqlite_repo: SQLiteTrackRepository):
        await sqlite_repo.update_enrichment(
            "FakeStation", "Unknown - Song",
            album="X",
        )

    async def test_close_releases_connection(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        # Subsequent operations should raise
        with pytest.raises(Exception):
            await repo.exists("x", "y")

    async def test_wal_mode_enabled(self, tmp_db_path: Path):
        SQLiteTrackRepository(tmp_db_path)
        # WAL file created on first write
        track = SavedTrack("A - B", "A", "B", "/x", 1)
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.register(track, "S")
        await repo.aclose()
        # Check journal mode
        import sqlite3
        conn = sqlite3.connect(str(tmp_db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"