"""Tests for radio_ripper.services.storage (stream — TrackWriter only)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from radio_ripper.services.storage import (
    TrackWriter,
    _parse_acoustid_response,
    build_metadata_filename,
    rename_track,
    sanitize_filename,
    write_mp3_tags,
)


class TestSanitizeFilename:
    def test_strip_illegal_chars(self):
        assert sanitize_filename("A/B:C*D") == "ABCD"

    def test_replace_newlines_with_space(self):
        assert sanitize_filename("foo\r\nbar") == "foo bar"

    def test_collapse_whitespace(self):
        assert sanitize_filename("  foo   bar  ") == "foo bar"

    def test_truncate_long(self):
        long_name = "A" * 300
        result = sanitize_filename(long_name)
        assert len(result) <= 200

    def test_none_returns_empty(self):
        assert sanitize_filename(None) == ""

    def test_blank_returns_empty(self):
        assert sanitize_filename("  ") == ""

    def test_after_stripping_illegal_chars_returns_empty(self):
        assert sanitize_filename('<>:"') == ""


class TestTrackWriter:
    def test_initial_size_zero(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        assert w.size == 0

    def test_write_increases_size(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        w.write(b"hello")
        assert w.size == 5

    def test_commit_moves_file(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"data")
        assert w.commit() is True
        assert dst.is_file()
        assert dst.read_bytes() == b"data"

    def test_commit_below_min_size_discards(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=100)
        w.write(b"small")
        assert w.commit() is False
        assert not dst.exists()

    def test_commit_twice_noop(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"x")
        assert w.commit() is True
        assert w.commit() is False

    def test_discard_cleans_temp(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst)
        tmp = w._tmp_path
        assert tmp.exists()
        w.discard()
        assert not tmp.exists()

    def test_context_manager_commits(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        with TrackWriter(dst, min_size=1) as w:
            w.write(b"data")
        assert dst.is_file()

    def test_context_manager_discards_on_error(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        with pytest.raises(ValueError), TrackWriter(dst, min_size=1) as w:
            w.write(b"data")
            raise ValueError("boom")
        assert not dst.exists()


class TestGetMp3Duration:
    @patch("shutil.which", return_value=None)
    async def test_ffprobe_not_available(self, mock_which, tmp_path):
        from radio_ripper.services.storage import get_mp3_duration

        result = await get_mp3_duration(tmp_path)
        assert result is None

    async def test_duration_nonexistent_file(self, tmp_path):
        from radio_ripper.services.storage import get_mp3_duration

        result = await get_mp3_duration(tmp_path / "nonexistent.mp3")
        assert result is None


class TestIsValidMp3:
    async def test_valid_mp3_header(self, tmp_path):
        path = tmp_path / "test.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is True

    async def test_random_data_not_mp3(self, tmp_path):
        path = tmp_path / "test.bin"
        path.write_bytes(b"\x00\x01\x02\x03" + b"\xab" * 100)
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is False

    async def test_empty_file_not_mp3(self, tmp_path):
        path = tmp_path / "empty.mp3"
        path.write_bytes(b"")
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is False

    async def test_nonexistent_file_returns_false(self, tmp_path):
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(tmp_path / "nope.mp3") is False


class TestTrackWriterAdvanced:
    def test_state_property(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        assert w.state == "open"
        w.write(b"x" * 100)
        w.commit()
        assert w.state == "committed"

    def test_flush(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        w.write(b"data")
        w.flush()
        assert w.size == 4

    def test_commit_flush_error_does_not_raise(self, tmp_path, caplog):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"data")
        with patch.object(w._fh, "flush", side_effect=OSError("disk error")):
            ok = w.commit()
        assert ok is True

    def test_discard_twice_noop(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst)
        w.discard()
        w.discard()

    def test_discard_after_commit_noop(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"data")
        w.commit()
        w.discard()
        assert dst.exists()


class TestGetMp3DurationAdvanced:
    async def test_duration_ffprobe_error(self, tmp_path):
        from radio_ripper.services.storage import get_mp3_duration

        path = tmp_path / "test.mp3"
        path.write_bytes(b"\x00" * 100)
        result = await get_mp3_duration(path)
        assert result is None or isinstance(result, float)


class TestParseAcoustidResponse:
    def _response(self, score: float, *, title: str = "Song", artist: str = "Artist") -> dict:
        return {
            "results": [
                {
                    "id": "r",
                    "score": score,
                    "recordings": [
                        {
                            "id": "rec",
                            "title": title,
                            "artists": [{"id": "a1", "name": artist}],
                        }
                    ],
                }
            ]
        }

    def test_selects_best_match(self):
        data = {
            "results": [
                {"id": "low", "score": 0.5, "recordings": [{"title": "Low", "artists": [{"name": "X"}]}]},
                {"id": "high", "score": 0.95, "recordings": [{"title": "Song", "artists": [{"name": "Artist"}]}]},
            ]
        }
        match = _parse_acoustid_response(data, min_score=0.9)
        assert match is not None
        assert match.artist == "Artist"
        assert match.title == "Song"
        assert match.score == pytest.approx(0.95)

    def test_below_threshold_returns_none(self):
        assert _parse_acoustid_response(self._response(0.5), min_score=0.9) is None

    def test_no_results_returns_none(self):
        assert _parse_acoustid_response({"status": "ok", "results": []}, min_score=0.9) is None

    def test_joins_multiple_artists(self):
        data = {
            "results": [
                {
                    "id": "r",
                    "score": 0.95,
                    "recordings": [
                        {
                            "id": "rec",
                            "title": "Song",
                            "artists": [{"name": "A"}, {"name": "B"}],
                        }
                    ],
                }
            ]
        }
        match = _parse_acoustid_response(data, min_score=0.9)
        assert match is not None
        assert match.artist == "A, B"

    def test_recording_without_metadata_is_skipped(self):
        data = {"results": [{"id": "r", "score": 0.95, "recordings": [{"id": "rec", "title": "", "artists": []}]}]}
        assert _parse_acoustid_response(data, min_score=0.9) is None

    def test_invalid_score_skipped(self):
        data = {"results": [{"id": "r", "score": "nope", "recordings": [{"title": "T", "artists": [{"name": "A"}]}]}]}
        assert _parse_acoustid_response(data, min_score=0.9) is None


class TestBuildMetadataFilename:
    def test_basic(self):
        assert build_metadata_filename("Artist", "Title") == "Artist - Title.mp3"

    def test_illegal_chars_stripped(self):
        assert build_metadata_filename("A/B", "Title") == "AB - Title.mp3"

    def test_empty_returns_empty(self):
        assert build_metadata_filename("", "") == ""
        assert build_metadata_filename("   ", "  ") == ""


class TestRenameTrack:
    def test_renames_file(self, tmp_path):
        src = tmp_path / "old - title.mp3"
        src.write_bytes(b"x")
        new = rename_track(src, "Artist", "Title")
        assert new.name == "Artist - Title.mp3"
        assert new.is_file()
        assert not src.exists()

    def test_no_rename_when_name_unchanged(self, tmp_path):
        src = tmp_path / "Artist - Title.mp3"
        src.write_bytes(b"x")
        assert rename_track(src, "Artist", "Title") == src

    def test_no_rename_when_empty_metadata(self, tmp_path):
        src = tmp_path / "song.mp3"
        src.write_bytes(b"x")
        assert rename_track(src, "", "") == src

    def test_renames_over_existing_target(self, tmp_path):
        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"keep")
        src = tmp_path / "other.mp3"
        src.write_bytes(b"new")
        new = rename_track(src, "Artist", "Title")
        assert new.name == "Artist - Title.mp3"
        assert new.is_file()
        assert new.read_bytes() == b"new"


class TestWriteMp3Tags:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert write_mp3_tags(path, artist="Artist", title="Title") is True

        from mutagen.id3 import ID3

        tags = ID3(path)
        assert str(tags["TPE1"]) == "Artist"
        assert str(tags["TIT2"]) == "Title"

    def test_missing_file_returns_false(self, tmp_path):
        assert write_mp3_tags(tmp_path / "nope.mp3", artist="A", title="T") is False

    def test_empty_fields_still_taggable(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert write_mp3_tags(path, artist="", title="") is True


class TestFinalizeWithMetadata:
    async def test_no_collision_renames_and_tags(self, tmp_path):
        from radio_ripper.services.storage import finalize_with_metadata

        src = tmp_path / "old.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()
        assert not src.exists()

        from mutagen.id3 import ID3

        tags = ID3(result)
        assert str(tags["TPE1"]) == "Artist"
        assert str(tags["TIT2"]) == "Title"

    async def test_existing_higher_score_is_kept(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key):
            return AcoustidLookup(accepted=True, match=AcoustidMatch("Artist", "Title", 0.99))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.9)
        assert result == existing
        assert existing.read_bytes() == b"old"
        assert not src.exists()

    async def test_existing_lower_score_is_replaced(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key):
            return AcoustidLookup(accepted=True, match=AcoustidMatch("Artist", "Title", 0.8))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()
        assert b"new" in result.read_bytes()
        assert not src.exists()

    async def test_tie_keeps_existing(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key):
            return AcoustidLookup(accepted=True, match=AcoustidMatch("Artist", "Title", 0.95))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result == existing
        assert not src.exists()

    async def test_existing_without_metadata_is_replaced(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key):
            return AcoustidLookup(accepted=True, match=None)

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()
        assert b"new" in result.read_bytes()
        assert not src.exists()
