"""Tests for radio_ripper.services.storage."""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import pydub.audio_segment  # noqa: F401 – pre-load so patches avoid DeprecationWarning

from radio_ripper.services.storage import (
    TrackWriter,
    compute_file_path,
    get_mp3_duration,
    remove_empty_parents,
    remux_mp3,
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

    def test_all_chars_removed_returns_unknown(self):
        assert sanitize_filename("<>:\"") == "unknown"


class TestComputeFilePath:
    def test_basic_path_no_album(self, tmp_path: Path):
        p = compute_file_path(tmp_path, "Adele", "Hello", "Adele - Hello")
        assert p == tmp_path / "Adele" / "Adele - Hello.mp3"

    def test_explicit_album(self, tmp_path: Path):
        p = compute_file_path(tmp_path, "Adele", "Hello", "Adele - Hello", album="25")
        assert p == tmp_path / "Adele" / "25" / "Adele - Hello.mp3"

    def test_no_artist_in_stream_title(self, tmp_path: Path):
        p = compute_file_path(tmp_path, "", "", "SimplyJonk")
        assert p == tmp_path / "Unknown" / "SimplyJonk.mp3"

    def test_avoid_collision(self, tmp_path: Path):
        first = compute_file_path(tmp_path, "A", "T", "A - T")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"")
        second = compute_file_path(tmp_path, "A", "T", "A - T")
        assert second == tmp_path / "A" / "A - T (2).mp3"

    def test_overwrite_flag_no_collision_suffix(self, tmp_path: Path):
        first = compute_file_path(tmp_path, "A", "T", "A - T")
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"")
        second = compute_file_path(tmp_path, "A", "T", "A - T", overwrite=True)
        assert second == first

    def test_multiple_collisions(self, tmp_path: Path):
        base = compute_file_path(tmp_path, "A", "T", "A - T")
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_bytes(b"")
        base2 = compute_file_path(tmp_path, "A", "T", "A - T")
        base2.write_bytes(b"")
        base3 = compute_file_path(tmp_path, "A", "T", "A - T")
        assert base3 == tmp_path / "A" / "A - T (3).mp3"


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

    def test_double_commit_returns_false(self, tmp_path: Path):
        target = tmp_path / "inside" / "double.mp3"
        target.parent.mkdir(parents=True)
        w = TrackWriter(target, min_size=10)
        w.write(b"x" * 100)
        assert w.commit() is True
        assert w.commit() is False

    def test_discard_after_discard_noop(self, tmp_path: Path):
        target = tmp_path / "discardx2.mp3"
        w = TrackWriter(target, min_size=10)
        w.write(b"x" * 100)
        w.discard()
        w.discard()

    def test_flush_does_not_error(self, tmp_path: Path):
        target = tmp_path / "flush.mp3"
        w = TrackWriter(target, min_size=10)
        w.write(b"test")
        w.flush()

    def test_commit_flush_error_suppressed(self, tmp_path: Path):
        target = tmp_path / "inside" / "flush-fail.mp3"
        target.parent.mkdir(parents=True)
        w = TrackWriter(target, min_size=10)
        w.write(b"x" * 100)
        with patch.object(w._fh, "flush", side_effect=OSError("disk full")):
            ok = w.commit()
        assert ok is True
        assert target.exists()

    def test_size_property(self, tmp_path: Path):
        target = tmp_path / "size.mp3"
        w = TrackWriter(target, min_size=10)
        assert w.size == 0
        w.write(b"abc")
        assert w.size == 3


class TestRemoveEmptyParents:
    def test_removes_empty_dirs(self, tmp_path: Path):
        root = tmp_path
        d = root / "a" / "b"
        d.mkdir(parents=True)
        fp = d / "track.mp3"
        fp.write_bytes(b"data")
        fp.unlink()
        remove_empty_parents(fp, root)
        assert not d.exists()

    def test_stops_at_root(self, tmp_path: Path):
        root = tmp_path / "base"
        root.mkdir()
        d = root / "sub"
        d.mkdir()
        fp = root / "track.mp3"
        fp.write_bytes(b"data")
        fp.unlink()
        remove_empty_parents(fp, root)
        assert root.exists()

    def test_non_empty_dir_not_removed(self, tmp_path: Path):
        root = tmp_path
        d = root / "a" / "b"
        d.mkdir(parents=True)
        fp = d / "track.mp3"
        fp.write_bytes(b"data")
        fp.unlink()
        (d / "other.txt").write_text("keep")
        remove_empty_parents(fp, root)
        assert d.exists()

    def test_oserror_breaks_loop(self, tmp_path: Path):
        root = tmp_path
        d = root / "a"
        d.mkdir()
        fp = d / "track.mp3"
        fp.write_bytes(b"data")
        fp.unlink()
        d.rmdir()
        # Already removed — next parent is root so nothing to do
        remove_empty_parents(fp, root)


class TestRemuxMp3:
    def test_import_error_is_silent(self, tmp_path: Path):
        p = tmp_path / "track.mp3"
        p.write_bytes(b"garbage")
        import builtins
        orig_import = builtins.__import__

        def _mock_import(name, *a: object, **kw: object) -> object:
            if name == "pydub":
                raise ImportError("no pydub")
            return orig_import(name, *a, **kw)

        with patch.object(builtins, "__import__", _mock_import):
            remux_mp3(p)
        assert p.read_bytes() == b"garbage"

    def test_exception_caught(self, tmp_path: Path) -> None:
        p = tmp_path / "track.mp3"
        p.write_bytes(b"data")
        with patch("pydub.AudioSegment") as mock_aseg:
            mock_aseg.from_file.side_effect = RuntimeError("ffmpeg fail")
            remux_mp3(p)

    def test_export_exception_cleans_up_temp(self, tmp_path: Path) -> None:
        p = tmp_path / "track.mp3"
        p.write_bytes(b"data")
        with patch("pydub.AudioSegment") as mock_aseg:
            seg = MagicMock()
            seg.export.side_effect = RuntimeError("export fail")
            mock_aseg.from_file.return_value = seg
            remux_mp3(p)
        assert not p.with_suffix(".remux.tmp").exists()


class TestGetMp3Duration:
    async def test_ffprobe_not_found(self, tmp_path: Path):
        with patch("shutil.which", return_value=None):
            assert get_mp3_duration(tmp_path / "x.mp3") is None

    async def test_ffprobe_nonzero_return(self, tmp_path: Path):
        fp = tmp_path / "x.mp3"
        fp.write_bytes(b"d")
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stdout = ""
                assert get_mp3_duration(fp) is None

    async def test_ffprobe_empty_output(self, tmp_path: Path):
        fp = tmp_path / "x.mp3"
        fp.write_bytes(b"d")
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = ""
                assert get_mp3_duration(fp) is None

    async def test_ffprobe_success(self, tmp_path: Path):
        fp = tmp_path / "x.mp3"
        fp.write_bytes(b"d")
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "203.5\n"
                assert get_mp3_duration(fp) == 203.5

    async def test_ffprobe_exception_returns_none(self, tmp_path: Path):
        fp = tmp_path / "x.mp3"
        fp.write_bytes(b"d")
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            with patch("subprocess.run", side_effect=OSError("no ffprobe")):
                assert get_mp3_duration(fp) is None

    async def test_ffprobe_value_error_returns_none(self, tmp_path: Path):
        fp = tmp_path / "x.mp3"
        fp.write_bytes(b"d")
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "not-a-float\n"
                assert get_mp3_duration(fp) is None
