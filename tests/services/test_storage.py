"""Tests for radio_ripper.services.storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from radio_ripper.services.storage import (
    TrackWriter,
    compute_file_path,
    sanitize_filename,
)


class TestSanitizeFilename:
    def test_strip_illegal_chars(self):
        assert sanitize_filename("A/B:C*D") == "ABCD"

    def test_replace_newlines_with_space(self):
        assert sanitize_filename("foo\r\nbar") == "foo bar"

    def test_collapse_whitespace(self):
        assert sanitize_filename("a   b") == "a b"

    def test_empty_returns_unknown(self):
        assert sanitize_filename("") == "unknown"
        assert sanitize_filename(None) == "unknown"

    def test_strip_trailing_underscores(self):
        assert sanitize_filename("  ") == "unknown"

    def test_truncates_long_name(self):
        result = sanitize_filename("a" * 300)
        assert len(result) == 200


class TestComputeFilePath:
    def test_basic_path(self, tmp_path: Path):
        p = compute_file_path(tmp_path, "Rock", "Adele", "Hello", "Adele - Hello")
        assert p == tmp_path / "Rock" / "Adele - Hello.mp3"

    def test_no_artist_in_stream_title(self, tmp_path: Path):
        p = compute_file_path(tmp_path, "Rock", "", "", "SimplyJonk")
        assert p == tmp_path / "Rock" / "SimplyJonk.mp3"

    def test_avoid_collision(self, tmp_path: Path):
        first = compute_file_path(tmp_path, "Rock", "A", "T", "A - T")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"")
        second = compute_file_path(tmp_path, "Rock", "A", "T", "A - T")
        assert second == tmp_path / "Rock" / "A - T (2).mp3"

    def test_overwrite_flag_no_collision_suffix(self, tmp_path: Path):
        first = compute_file_path(tmp_path, "Rock", "A", "T", "A - T")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"")
        second = compute_file_path(
            tmp_path, "Rock", "A", "T", "A - T", overwrite=True
        )
        assert second == first


class TestTrackWriter:
    def test_commit_keeps_file_and_size(self, tmp_path: Path):
        target = tmp_path / "x" / "song.mp3"
        target.parent.mkdir(parents=True)
        w = TrackWriter(target, min_size=10)
        w.write(b"x" * 100)
        ok = w.commit()
        assert ok
        assert target.exists()
        assert target.stat().st_size == 100

    def test_discard_too_small_skips_file(self, tmp_path: Path):
        target = tmp_path / "small.mp3"
        w = TrackWriter(target, min_size=1024)
        w.write(b"x" * 5)
        ok = w.commit()
        assert ok is False
        assert not target.exists()
        assert not w.final_path.with_suffix(".mp3.tmp").exists()

    def test_discard_incomplete(self, tmp_path: Path):
        target = tmp_path / "inc.mp3"
        w = TrackWriter(target, min_size=10)
        w.write(b"x" * 50)
        w.discard()
        assert not target.exists()
        assert not target.with_suffix(".mp3.tmp").exists()

    def test_context_manager_success(self, tmp_path: Path):
        target = tmp_path / "ctx.mp3"
        with TrackWriter(target, min_size=10) as w:
            w.write(b"y" * 20)
        assert target.exists()
        assert target.stat().st_size == 20

    def test_context_manager_exception_discards(self, tmp_path: Path):
        target = tmp_path / "ctx-err.mp3"
        with pytest.raises(RuntimeError), TrackWriter(target, min_size=10) as w:
            w.write(b"y" * 20)
            raise RuntimeError("boom")
        assert not target.exists()
        assert not target.with_suffix(".mp3.tmp").exists()
