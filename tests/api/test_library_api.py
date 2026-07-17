"""Tests for radio_ripper.api.library_api."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from radio_ripper.api.library_api import LibraryApi, SongInfo
from radio_ripper.infra.config import Settings, StreamConfig


@pytest.fixture
def library(tmp_path: Path) -> tuple[LibraryApi, sqlite3.Connection]:
    db_path = tmp_path / "songs.db"
    dest = tmp_path / "recordings"
    dest.mkdir()
    settings = Settings(
        destination=dest,
        database=db_path,
        streams=[StreamConfig(name="TopHits", url="http://x/listen.m3u")],
    )
    # Create test songs
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS songs (
            id, station_name, stream_title, artist, title,
            album, year, file_path, file_size, has_cover, enrichment, created_at
        )
    """)
    conn.executemany(
        "INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "TopHits",
                "Adele - Hello",
                "Adele",
                "Hello",
                "25",
                "2015",
                str(dest / "TopHits" / "Adele - Hello.mp3"),
                4096,
                1,
                "",
                "2026-01-01 12:00:00",
            ),
            (
                2,
                "TopHits",
                "Queen - Bohemian Rhapsody",
                "Queen",
                "Bohemian Rhapsody",
                "A Night at the Opera",
                "1975",
                str(dest / "TopHits" / "Queen - Bohemian Rhapsody.mp3"),
                8192,
                0,
                "",
                "2026-01-02 12:00:00",
            ),
            (
                3,
                "Rock",
                "ACDC - Back in Black",
                "ACDC",
                "Back in Black",
                "Back in Black",
                "1980",
                str(dest / "Rock" / "ACDC - Back in Black.mp3"),
                2048,
                0,
                "",
                "2026-01-03 12:00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()
    return LibraryApi(settings), None


class TestLibraryApi:
    def test_list_songs_returns_all(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        songs = api.list_songs()
        assert len(songs) == 3
        # newest first by created_at DESC
        assert songs[0].id == 3
        assert songs[2].id == 1

    def test_list_songs_is_song_info(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        songs = api.list_songs()
        assert isinstance(songs[0], SongInfo)
        assert songs[0].artist == "ACDC"

    def test_search_songs_by_artist(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        songs = api.search_songs("Adele")
        assert len(songs) == 1
        assert songs[0].title == "Hello"

    def test_search_songs_by_title(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        songs = api.search_songs("Bohemian")
        assert len(songs) == 1
        assert songs[0].artist == "Queen"

    def test_search_songs_by_station(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        songs = api.search_songs("Rock")
        assert len(songs) == 1  # station_name="Rock"
        assert songs[0].station_name == "Rock"

    def test_get_song_by_id(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        song = api.get_song(2)
        assert song is not None
        assert song.artist == "Queen"

    def test_get_song_invalid_id(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        assert api.get_song(999) is None

    def test_resolve_absolute_path(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        song = api.get_song(1)
        assert song is not None
        # file doesn't exist on disk → absolute_path should be None
        assert song.absolute_path is None

    def test_resolve_existing_file(self, library: tuple[LibraryApi, None], tmp_path: Path) -> None:
        api, _ = library
        dest = tmp_path / "recordings"
        station_dir = dest / "TopHits"
        station_dir.mkdir(parents=True, exist_ok=True)
        mp3 = station_dir / "Adele - Hello.mp3"
        mp3.write_bytes(b"\x00")
        # get_song uses stored file_path which is absolute,
        # so resolution should return the absolute path when the file exists.
        song = api.get_song(1)
        assert song is not None
        assert song.absolute_path is not None
        assert Path(song.absolute_path).is_file()

    def test_delete_song_removes_db_and_file(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        # Create an actual file for song 1
        dest = Path(api._destination)
        mp3 = dest / "TopHits" / "Adele - Hello.mp3"
        mp3.parent.mkdir(parents=True, exist_ok=True)
        mp3.write_bytes(b"\x00" * 100)
        assert mp3.is_file()
        ok = api.delete_song(1)
        assert ok
        assert not mp3.is_file()
        assert api.get_song(1) is None

    def test_delete_invalid_id_returns_false(self, library: tuple[LibraryApi, None]) -> None:
        api, _ = library
        assert api.delete_song(999) is False
