"""Tests for radio_ripper.services.tagging."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.id3 import ID3

from radio_ripper.domain.models import EnrichedInfo, TrackInfo
from radio_ripper.infra.errors import TaggingError
from radio_ripper.services.tagging import ID3Tagger, NullTagger


def _write_blank_mp3(path: Path, size: int = 4096) -> None:
    """Write minimal non-empty MP3-like data so ID3() can load it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb" + b"\x00" * (size - 2))


class TestID3Tagger:
    def test_write_basic_tags(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        tagger.write_basic(f, track, "Rock@http://x")
        audio = ID3(f)
        assert audio.get("TPE1").text == ["Adele"]
        assert audio.get("TIT2").text == ["Hello"]
        assert audio.get("COMM::eng").text == ["Recorded via Radio-Ripper"]
        assert audio.get("TXXX:RIPPEDBY").text == ["Rock@http://x"]

    def test_write_basic_without_artist(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Jingle", artist="", title="Jingle")
        tagger.write_basic(f, track, "Station@url")
        audio = ID3(f)
        assert "TPE1" not in audio
        assert audio.get("TIT2").text == ["Jingle"]

    def test_write_full_tags_with_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        enriched = EnrichedInfo(
            artist="Adele",
            title="Hello",
            album="25",
            year="2015",
            genre="Pop",
        )
        cover = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        tagger.write_full(f, track, enriched, cover, "Rock@url")
        audio = ID3(f)
        assert audio.get("TALB").text == ["25"]
        assert str(audio.get("TDRC").text[0]) == "2015"
        apic = audio.get("APIC:Cover")
        assert apic is not None
        assert apic.mime == "image/jpeg"
        assert apic.data == cover

    def test_write_full_tags_without_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        enriched = EnrichedInfo(artist="A", title="B", album="alb", year="2020")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert str(audio.get("TDRC").text[0]) == "2020"
        assert "APIC:Cover" not in audio

    def test_write_full_prefers_enriched_over_track(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Old - Old", artist="Old", title="Old")
        enriched = EnrichedInfo(artist="New", title="NewT")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TPE1").text == ["New"]
        assert audio.get("TIT2").text == ["NewT"]

    def test_write_overwrites_previous_tags(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track1 = TrackInfo("A - X", "A", "X")
        tagger.write_basic(f, track1, "S@u")
        track2 = TrackInfo("B - Y", "B", "Y")
        tagger.write_basic(f, track2, "S@u")
        audio = ID3(f)
        assert audio.get("TPE1").text == ["B"]
        assert audio.get("TIT2").text == ["Y"]

    def test_guess_image_mime_jpeg(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"

    def test_guess_image_mime_png(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_guess_image_mime_gif(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"GIF8") == "image/gif"

    def test_guess_image_mime_defaults_jpeg(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"\x00\x01\x02") == "image/jpeg"

    def test_write_to_nonexistent_file_raises_tagging_error(self, tmp_path: Path):
        tagger = ID3Tagger()
        f = tmp_path / "nonexistent_dir" / "song.mp3"
        track = TrackInfo("A - B", "A", "B")
        with pytest.raises(TaggingError):
            tagger.write_basic(f, track, "S@u")

    def test_write_full_to_nonexistent_file_raises_tagging_error(self, tmp_path: Path):
        tagger = ID3Tagger()
        f = tmp_path / "nonexistent_dir" / "song.mp3"
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A")
        with pytest.raises(TaggingError):
            tagger.write_full(f, track, enriched, None, "S@u")


class TestNullTagger:
    def test_write_basic_does_nothing(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        original = f.read_bytes()
        tagger = NullTagger()
        track = TrackInfo("A - B", "A", "B")
        tagger.write_basic(f, track, "S@u")
        assert f.read_bytes() == original

    def test_write_full_does_nothing(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        original = f.read_bytes()
        tagger = NullTagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A")
        tagger.write_full(f, track, enriched, b"cover", "S@u")
        assert f.read_bytes() == original
