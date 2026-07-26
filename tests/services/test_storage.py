"""Tests for radio_ripper.services.storage (stream — TrackWriter only)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from radio_ripper.services.storage import TrackWriter, sanitize_filename


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
        path.write_bytes(b"\xFF\xFB\x90\x00" + b"\x00" * 100)
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is True

    async def test_random_data_not_mp3(self, tmp_path):
        path = tmp_path / "test.bin"
        path.write_bytes(b"\x00\x01\x02\x03" + b"\xAB" * 100)
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
