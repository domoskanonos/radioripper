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

    def test_delete_missing_file_still_succeeds(self, library: tuple[LibraryApi, None]) -> None:
        """delete_song returns True even when the MP3 file doesn't exist on disk."""
        api, _ = library
        ok = api.delete_song(1)
        assert ok
        assert api.get_song(1) is None

    def test_resolve_path_fallback_returns_none(self, tmp_path: Path) -> None:
        """A non-existent relative file_path reaches the final return None."""
        db_path = tmp_path / "test.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE songs (id, station_name, stream_title, artist, title, "
            "album, year, file_path, file_size, has_cover, enrichment, created_at)"
        )
        conn.execute(
            "INSERT INTO songs VALUES (1,'Test','Song','A','B','','',"
            "'nonexistent/subdir/song.mp3',100,0,'','2026-01-01')"
        )
        conn.commit()
        conn.close()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        song = api.get_song(1)
        assert song is not None
        assert song.absolute_path is None

    def test_delete_song_missing_file_after_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """delete_song covers the p.is_file() = False branch (line 113)."""
        db_path = tmp_path / "test.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE songs (id, station_name, stream_title, artist, title, "
            "album, year, file_path, file_size, has_cover, enrichment, created_at)"
        )
        conn.execute(
            "INSERT INTO songs VALUES (1,'Test','Song','A','B','','',"
            "'/nonexistent.mp3',100,0,'','2026-01-01')"
        )
        conn.commit()
        conn.close()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        raw_song = SongInfo(
            id=1,
            station_name="Test",
            stream_title="Song",
            artist="A",
            title="B",
            album=None,
            year=None,
            file_path="/nonexistent.mp3",
            file_size=100,
            has_cover=False,
            created_at="2026-01-01",
            absolute_path="/nonexistent.mp3",
        )

        def _fake_get(song_id: int) -> SongInfo | None:
            return raw_song if song_id == 1 else None

        monkeypatch.setattr(api, "get_song", _fake_get)
        assert api.delete_song(1) is True


class TestEmptyDatabase:
    """All API methods behave gracefully when the songs table doesn't exist."""

    def test_list_songs_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        assert api.list_songs() == []

    def test_search_songs_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        assert api.search_songs("test") == []

    def test_get_song_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        assert api.get_song(1) is None

    def test_delete_song_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        assert api.delete_song(1) is False

    def test_delete_song_missing_table_after_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """delete_song reaches line 109 when the songs table is dropped
        between get_song and the DELETE connection."""
        db_path = tmp_path / "test.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        # No songs table → get_song returns None naturally.
        # Monkeypatch get_song so it returns a fake SongInfo (bypassing the
        # real DB check), then the inner self._connect() will find no table.
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        real_get = api.get_song

        def _fake_get(song_id: int) -> SongInfo | None:
            if song_id == 1:
                return SongInfo(
                    id=1,
                    station_name="Test",
                    stream_title="Song",
                    artist="A",
                    title="B",
                    album=None,
                    year=None,
                    file_path="/nonexistent.mp3",
                    file_size=100,
                    has_cover=False,
                    created_at="2026-01-01",
                    absolute_path=None,
                )
            return real_get(song_id)

        monkeypatch.setattr(api, "get_song", _fake_get)
        assert api.delete_song(1) is False


class TestResolvePath:
    """Edge cases in _resolve_path."""

    def test_empty_file_path_returns_none(self, tmp_path: Path) -> None:
        """A song with an empty file_path returns absolute_path=None."""
        db_path = tmp_path / "test.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE songs (id, station_name, stream_title, artist, title, "
            "album, year, file_path, file_size, has_cover, enrichment, created_at)"
        )
        conn.execute("INSERT INTO songs VALUES (1,'Test','','','','','','',0,0,'','2026-01-01')")
        conn.commit()
        conn.close()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        song = api.get_song(1)
        assert song is not None
        assert song.absolute_path is None
        assert song.file_path == ""

    def test_relative_path_resolves_via_destination(self, tmp_path: Path) -> None:
        """A relative file_path is resolved relative to destination."""
        db_path = tmp_path / "test.db"
        dest = tmp_path / "recordings"
        dest.mkdir(parents=True)
        sub = dest / "subdir"
        sub.mkdir()
        mp3 = sub / "song.mp3"
        mp3.write_bytes(b"\x00")
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE songs (id, station_name, stream_title, artist, title, "
            "album, year, file_path, file_size, has_cover, enrichment, created_at)"
        )
        conn.execute(
            "INSERT INTO songs VALUES (1,'Test','Song','A','B','','',"
            "'subdir/song.mp3',100,0,'','2026-01-01')"
        )
        conn.commit()
        conn.close()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        song = api.get_song(1)
        assert song is not None
        assert song.absolute_path is not None
        assert Path(song.absolute_path).is_file()

    def test_cwd_relative_path_resolves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A CWD-relative file path resolves when the file exists."""
        db_path = tmp_path / "test.db"
        dest = tmp_path / "recordings"
        dest.mkdir()
        monkeypatch.chdir(tmp_path)
        mp3 = Path("cwd_song.mp3")
        mp3.write_bytes(b"\x00")
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE songs (id, station_name, stream_title, artist, title, "
            "album, year, file_path, file_size, has_cover, enrichment, created_at)"
        )
        conn.execute(
            "INSERT INTO songs VALUES (1,'Test','Song','A','B','','',"
            "'cwd_song.mp3',100,0,'','2026-01-01')"
        )
        conn.commit()
        conn.close()
        settings = Settings(
            destination=dest,
            database=db_path,
        )
        api = LibraryApi(settings)
        song = api.get_song(1)
        assert song is not None
        assert song.absolute_path is not None
        assert Path(song.absolute_path).is_file()
