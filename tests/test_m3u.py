"""Tests für radio_ripper.m3u — M3U-Parsing & Stationen laden."""

from __future__ import annotations

from pathlib import Path

import pytest

from radio_ripper.config import Settings
from radio_ripper.m3u import load_stations, parse_m3u_entries


def test_parse_m3u_entries() -> None:
    text = (
        "#EXTM3U\n#EXTINF:-1,Sender A\nhttp://a.example/stream.mp3\n#EXTINF:-1,Sender B\nhttp://b.example/stream.mp3\n"
    )
    entries = parse_m3u_entries(text, source="test")
    assert len(entries) == 2
    assert entries[0].name == "Sender A"
    assert entries[0].url == "http://a.example/stream.mp3"
    assert entries[0].source == "test"


def test_parse_m3u_entries_skips_comments() -> None:
    text = "#EXTM3U\n# ein Kommentar\n#EXTINF:-1,A\nhttp://a\n"
    assert len(parse_m3u_entries(text)) == 1


@pytest.mark.asyncio
async def test_load_stations(tmp_path: Path) -> None:
    stations_dir = tmp_path / "stations"
    stations_dir.mkdir(parents=True)
    (stations_dir / "custom.m3u").write_text("#EXTM3U\n#EXTINF:-1,A\nhttp://a\n#EXTINF:-1,B\nhttp://b\n")
    settings = Settings(work_dir=tmp_path)
    stations = await load_stations(settings)
    assert len(stations) == 2
    assert stations[0].name == "A"
    assert str(stations[0].url) == "http://a/"


@pytest.mark.asyncio
async def test_load_stations_missing_file(tmp_path: Path) -> None:
    settings = Settings(work_dir=tmp_path)
    assert await load_stations(settings) == []
