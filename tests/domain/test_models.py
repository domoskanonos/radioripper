from __future__ import annotations

from radio_ripper.domain.models import StreamMetadata, TrackInfo


class TestTrackInfo:
    def test_split_with_dash(self) -> None:
        t = TrackInfo.from_stream_title("Adele - Hello")
        assert t.artist == "Adele"
        assert t.title == "Hello"
        assert t.stream_title == "Adele - Hello"

    def test_split_with_long_dash(self) -> None:
        t = TrackInfo.from_stream_title("Artist — Song")
        assert t.artist == "Artist"
        assert t.title == "Song"

    def test_no_separator(self) -> None:
        t = TrackInfo.from_stream_title("Station Jingle")
        assert t.artist == ""
        assert t.title == "Station Jingle"

    def test_empty_string(self) -> None:
        t = TrackInfo.from_stream_title("")
        assert t.artist == ""
        assert t.title == ""

    def test_whitespace_only(self) -> None:
        t = TrackInfo.from_stream_title("   ")
        assert t.artist == ""
        assert t.title == ""


class TestStreamMetadata:
    def test_default_values(self) -> None:
        m = StreamMetadata(stream_title="Test", artist="A", title="B")
        assert m.stream_title == "Test"
        assert m.artist == "A"
        assert m.title == "B"
        assert m.metaint == 0
        assert m.bitrate == 0

    def test_full_init(self) -> None:
        m = StreamMetadata(stream_title="S", artist="A", title="T", metaint=8192, bitrate=128)
        assert m.metaint == 8192
        assert m.bitrate == 128

    def test_frozen(self) -> None:
        m = StreamMetadata(stream_title="S", artist="A", title="T")
        import pytest

        with pytest.raises(AttributeError):
            m.title = "X"  # type: ignore[misc]

    def test_track_info_from_stream_title(self) -> None:
        m = StreamMetadata(stream_title="Artist - Song", artist="Artist", title="Song", metaint=8192, bitrate=128)
        info = TrackInfo.from_stream_title(m.stream_title)
        assert info.artist == m.artist
        assert info.title == m.title
