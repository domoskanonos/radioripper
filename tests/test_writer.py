"""Tests für radio_ripper.writer — TrackWriter & sanitize_filename."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from radio_ripper.writer import TrackWriter, sanitize_filename


def test_sanitize_filename_illegal_chars() -> None:
    assert sanitize_filename("A/B:C*D") == "ABCD"
    assert sanitize_filename("  spaced  out  ") == "spaced out"
    assert sanitize_filename("") == ""
    assert sanitize_filename(None) == ""


def test_sanitize_filename_200_limit() -> None:
    long_name = "x" * 250
    assert len(sanitize_filename(long_name)) == 200


def test_sanitize_filename_newlines() -> None:
    assert sanitize_filename("line1\nline2") == "line1 line2"


def test_track_writer_commit(tmp_path: Path) -> None:
    """Schreibt .part → committet zu .mp3."""
    final = tmp_path / "song.mp3"
    writer = TrackWriter(final)
    writer.write(b"x" * 100)
    assert writer.size == 100
    assert writer.state == "open"

    assert writer.commit() is True
    assert writer.state == "committed"
    assert final.exists()
    assert not final.with_suffix(".part").exists()


def test_track_writer_discard(tmp_path: Path) -> None:
    """Discard entfernt .part und lässt kein .mp3 zurück."""
    final = tmp_path / "song.mp3"
    writer = TrackWriter(final)
    writer.write(b"x" * 10)
    writer.discard()
    assert writer.state == "discarded"
    assert not final.exists()
    assert not final.with_suffix(".part").exists()


def test_track_writer_commit_twice_returns_false(tmp_path: Path) -> None:
    """commit() nach commit() ist ein No-op und liefert False."""
    final = tmp_path / "song.mp3"
    writer = TrackWriter(final)
    writer.write(b"x" * 10)
    assert writer.commit() is True
    assert writer.commit() is False


def test_track_writer_discard_after_commit_noop(tmp_path: Path) -> None:
    """discard() nach commit() ändert nichts."""
    final = tmp_path / "song.mp3"
    writer = TrackWriter(final)
    writer.write(b"x" * 10)
    writer.commit()
    writer.discard()
    assert writer.state == "committed"
    assert final.exists()


def test_track_writer_commit_flush_error(tmp_path: Path) -> None:
    """Fehler beim Schließen → commit False, .part gelöscht."""
    final = tmp_path / "song.mp3"
    writer = TrackWriter(final)
    writer.write(b"x" * 10)
    with patch.object(writer, "_fh") as mock_fh:
        mock_fh.flush.side_effect = OSError("disk full")
        assert writer.commit() is False
    assert not final.exists()
    assert not final.with_suffix(".part").exists()


def test_track_writer_commit_replace_error(tmp_path: Path) -> None:
    """Fehler bei os.replace → commit False, .part gelöscht."""
    final = tmp_path / "song.mp3"
    writer = TrackWriter(final)
    writer.write(b"x" * 10)
    with (
        patch("os.replace", side_effect=OSError("permission denied")),
        patch("radio_ripper.writer.os.replace", side_effect=OSError("permission denied")),
    ):
        assert writer.commit() is False
    assert not final.exists()
