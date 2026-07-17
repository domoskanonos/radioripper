"""Tests for radio_ripper.services.icy — IcyParser state machine."""

from __future__ import annotations

import pytest

from radio_ripper.infra.errors import StreamProtocolError
from radio_ripper.services.icy import (
    AudioChunk,
    IcyParser,
    TitleChanged,
    split_track_info,
)


def _make_meta_block(stream_title: str) -> bytes:
    """Build a single ICY metadata block (length-prefixed)."""
    payload = f"StreamTitle='{stream_title}';".encode("utf-8")
    padding = (16 - (len(payload) % 16)) % 16
    payload += b"\x00" * padding
    length_byte = len(payload) // 16
    return bytes([length_byte]) + payload


def _make_stream(metaint: int, titles: list[str]) -> bytes:
    """Build a byte stream with alternating audio + metadata blocks."""
    data = bytearray()
    for title in titles:
        data.extend(b"\x01" * metaint)
        data.extend(_make_meta_block(title))
    return bytes(data)


class TestIcyParserBasics:
    def test_invalid_metaint_rejected(self):
        with pytest.raises(ValueError):
            IcyParser(0)
        with pytest.raises(ValueError):
            IcyParser(-10)

    def test_initial_state(self):
        p = IcyParser(100)
        assert p.state.name == "WAIT_AUDIO"

    def test_audio_chunk_event(self):
        p = IcyParser(64)
        p.feed(b"\x01" * 64)
        events = list(p.events())
        assert len(events) == 1
        assert isinstance(events[0], AudioChunk)
        assert events[0].data == b"\x01" * 64

    def test_title_change_event(self):
        p = IcyParser(64)
        p.feed(b"\x01" * 64 + _make_meta_block("Adele - Hello"))
        events = list(p.events())
        assert len(events) == 2
        assert isinstance(events[0], AudioChunk)
        assert isinstance(events[1], TitleChanged)
        assert events[1].title == "Adele - Hello"


class TestIcyParserStreaming:
    def test_multiple_songs(self):
        metaint = 100
        stream = _make_stream(metaint, ["A - X", "B - Y", "C - Z"])
        p = IcyParser(metaint)
        p.feed(stream)
        titles = [e.title for e in p.events() if isinstance(e, TitleChanged)]
        assert titles == ["A - X", "B - Y", "C - Z"]

    def test_empty_meta_block_no_title(self):
        metaint = 50
        empty_meta = bytes([0])  # length byte = 0 -> 0 bytes of metadata
        stream = b"\x01" * metaint + empty_meta + b"\x02" * metaint + _make_meta_block("A - B")
        p = IcyParser(metaint)
        p.feed(stream)
        titles = [e.title for e in p.events() if isinstance(e, TitleChanged)]
        assert titles == ["A - B"]

    def test_partial_metadata_accumulates(self):
        metaint = 32
        meta = _make_meta_block("A - B")
        p = IcyParser(metaint)
        p.feed(b"\x01" * metaint + meta[:3])
        events1 = list(p.events())
        assert not any(isinstance(e, TitleChanged) for e in events1)
        p.feed(meta[3:])
        events2 = list(p.events())
        titles = [e.title for e in events2 if isinstance(e, TitleChanged)]
        assert titles == ["A - B"]

    def test_chunked_audio(self):
        metaint = 100
        stream = b"\x01" * metaint + _make_meta_block("A - B")
        p = IcyParser(metaint)
        for i in range(0, len(stream), 37):
            p.feed(stream[i:i + 37])
        events = list(p.events())
        titles = [e.title for e in events if isinstance(e, TitleChanged)]
        assert titles == ["A - B"]

    def test_protocol_error_on_oversized_meta(self):
        metaint = 32
        p = IcyParser(metaint, max_meta_len=16)
        p.feed(b"\x01" * metaint + bytes([2]))  # length = 2*16 = 32 > 16
        with pytest.raises(StreamProtocolError):
            list(p.events())


class TestParseStreamTitle:
    def test_basic_title(self):
        meta = _make_meta_block("Adele - Hello")
        metaint = len(meta) + 10
        p = IcyParser(metaint)
        p.feed(b"\x00" * metaint + meta)
        events = list(p.events())
        titles = [e.title for e in events if isinstance(e, TitleChanged)]
        assert titles == ["Adele - Hello"]

    def test_empty_metadata_block(self):
        p = IcyParser(16)
        p.feed(b"\x00" * 16 + bytes([0]))
        events = list(p.events())
        assert not any(isinstance(e, TitleChanged) for e in events)

    def test_escaped_quote_in_title(self):
        meta = _make_meta_block("It\\'s Me")
        metaint = 32
        p = IcyParser(metaint)
        p.feed(b"\x00" * metaint + meta)
        events = list(p.events())
        titles = [e.title for e in events if isinstance(e, TitleChanged)]
        assert titles == ["It's Me"]


class TestSplitTrackInfo:
    def test_split_dash(self):
        info = split_track_info("Adele - Hello")
        assert info.artist == "Adele"
        assert info.title == "Hello"

    def test_no_dash_full_title(self):
        info = split_track_info("Station Jingle")
        assert info.artist == ""
        assert info.title == "Station Jingle"