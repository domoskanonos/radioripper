"""Tests für radio_ripper.writer — TrackWriter & sanitize_filename."""

from __future__ import annotations

from pathlib import Path

from radio_ripper.writer import TrackWriter, sanitize_filename


def test_sanitize_filename_illegal_chars() -> None:
    assert sanitize_filename('A/B:C*D') == "ABCD"
    assert sanitize_filename('  spaced  out  ') == "spaced out"
    assert sanitize_filename('') == ""
    assert sanitize_filename(None) == ""


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
