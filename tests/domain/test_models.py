"""Tests for radio_ripper.domain.models."""

from __future__ import annotations

from radio_ripper.domain.models import EnrichedInfo, SavedTrack, TrackInfo


class TestTrackInfo:
    def test_split_standard_dash(self):
        t = TrackInfo.from_stream_title("Adele - Hello")
        assert t.stream_title == "Adele - Hello"
        assert t.artist == "Adele"
        assert t.title == "Hello"

    def test_split_em_dash(self):
        t = TrackInfo.from_stream_title("Adele — Hello")
        assert t.artist == "Adele"
        assert t.title == "Hello"

    def test_no_separator_keeps_full_title(self):
        t = TrackInfo.from_stream_title(" sender ident ")
        assert t.artist == ""
        assert t.title == "sender ident"

    def test_strip_applied(self):
        t = TrackInfo.from_stream_title("   Adele - Hello   ")
        assert t.artist == "Adele"
        assert t.title == "Hello"

    def test_frozen_dataclass(self):
        t = TrackInfo("x", "a", "b")
        try:
            t.artist = "y"
        except AttributeError:
            pass
        else:
            raise AssertionError("TrackInfo should be frozen")


class TestEnrichedInfo:
    def test_defaults_all_none(self):
        e = EnrichedInfo()
        assert e.artist is None
        assert e.album is None
        assert e.artwork_url is None

    def test_partial(self):
        e = EnrichedInfo(artist="A", title="T")
        assert e.artist == "A"
        assert e.title == "T"


class TestSavedTrack:
    def test_defaults(self):
        s = SavedTrack(stream_title="A - B", artist="A", title="B", file_path="x", file_size=10)
        assert s.album is None
        assert s.has_cover is False
        assert s.enrichment is None
        assert s.extras == {}

    def test_with_enrichment(self):
        s = SavedTrack(
            stream_title="A - B",
            artist="A",
            title="B",
            file_path="x",
            file_size=10,
            album="alb",
            year="2020",
            has_cover=True,
            enrichment="itunes",
        )
        assert s.album == "alb"
        assert s.has_cover is True
        assert s.enrichment == "itunes"
