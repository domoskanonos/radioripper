"""Tests für radio_ripper.acoustid — Tagging, Ordnerstruktur, Collision."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.acoustid import (
    build_metadata_filename,
    build_target_path,
    finalize_acoustid,
    read_mp3_score,
    write_mp3_tags,
)
from radio_ripper.config import Settings
from radio_ripper.models import AcoustidMatch


def test_build_metadata_filename() -> None:
    assert build_metadata_filename("AC/DC", "Highway to Hell") == "ACDC - Highway to Hell.mp3"


def test_build_target_path_with_album(tmp_path: Path) -> None:
    target = build_target_path(tmp_path, "Queen", "Bo Rhap", "A Night at the Opera")
    assert str(target.relative_to(tmp_path)) == "Queen/A Night at the Opera/Queen - Bo Rhap.mp3"


def test_build_target_path_without_album(tmp_path: Path) -> None:
    target = build_target_path(tmp_path, "Queen", "Bo Rhap")
    assert str(target.relative_to(tmp_path)) == "Queen/Queen - Bo Rhap.mp3"


def test_build_target_path_unknown_artist(tmp_path: Path) -> None:
    target = build_target_path(tmp_path, "", "Song")
    assert target.parent.name == "Unknown Artist"


def test_write_and_read_mp3_tags(tmp_path: Path) -> None:
    mp3 = tmp_path / "tag.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)

    write_mp3_tags(
        mp3,
        artist="Artist",
        title="Title",
        album="Album",
        score=0.95,
        confirmations=7,
        recording_id="rec-id",
        releasegroup_id="rg-id",
    )
    assert read_mp3_score(mp3) == pytest.approx(0.95)


def test_finalize_acoustid_no_match_deletes(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x" * 100)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    with patch("radio_ripper.acoustid.acoustid_lookup", return_value=(None, "ok")):
        finalize_acoustid(mp3, settings)
    assert not mp3.exists(), "Kein Treffer → Datei gelöscht"


def test_finalize_acoustid_api_error_keeps(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x" * 100)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    with patch("radio_ripper.acoustid.acoustid_lookup", return_value=(None, "error")):
        finalize_acoustid(mp3, settings)
    assert mp3.exists(), "API-Fehler → Datei bleibt erhalten"


def test_finalize_acoustid_match_moves_and_tags(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    match = AcoustidMatch(
        artist="Queen",
        title="Bo Rhap",
        album="Album",
        score=0.95,
        recording_id="rec-id",
        releasegroup_id="rg-id",
    )
    with patch("radio_ripper.acoustid.acoustid_lookup", return_value=(match, "ok")):
        finalize_acoustid(mp3, settings)

    target = tmp_path / "dest" / "Queen" / "Album" / "Queen - Bo Rhap.mp3"
    assert target.exists(), "Treffer → Datei verschoben"
    assert read_mp3_score(target) == pytest.approx(0.95)


def test_finalize_acoustid_collision_keeps_better_score(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    target = dest / "Artist" / "Song.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    write_mp3_tags(target, artist="Artist", title="Song", score=0.99)

    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    settings = Settings(work_dir=tmp_path, destination=dest, acoustid_api_key="KEY")

    match = AcoustidMatch(artist="Artist", title="Song", album="", score=0.90)
    with patch("radio_ripper.acoustid.acoustid_lookup", return_value=(match, "ok")):
        finalize_acoustid(mp3, settings)

    assert target.exists(), "Bessere bestehende Datei bleibt"
    assert not mp3.exists(), "Schlechtere neue Datei wird verworfen"
