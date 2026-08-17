"""Tests für radio_ripper.icy — ICY-Metadaten-Parser."""

from __future__ import annotations

import pytest

from radio_ripper.icy import AudioChunk, IcyParser, TitleChanged


def test_parser_audio_and_title_events() -> None:
    """Parst Audio-Chunks und Titelwechsel aus einem ICY-Stream."""
    parser = IcyParser(metaint=16)
    # 16 Audio-Bytes + Längenbyte (2 = 32 Meta-Bytes) + exakt 32 Meta-Bytes
    meta = b"StreamTitle='Artist - Song';" + b"\x00" * 4  # 28 + 4 = 32
    chunk = b"A" * 16 + bytes([len(meta) // 16]) + meta + b"B" * 16
    parser.feed(chunk)

    events = parser.events()
    audio = [e for e in events if isinstance(e, AudioChunk)]
    titles = [e for e in events if isinstance(e, TitleChanged)]
    assert audio
    assert audio[0].data == b"A" * 16
    assert titles and titles[0].title == "Artist - Song"


def test_parser_metaint_zero_rejected() -> None:
    with pytest.raises(ValueError):
        IcyParser(metaint=0)


def test_parser_title_from_escaped_quote() -> None:
    """Anführungszeichen im Titel werden korrekt entschlüsselt."""
    parser = IcyParser(metaint=8)
    meta = b"StreamTitle='Don\\'t Stop';" + b"\x00" * 16
    chunk = b"B" * 8 + bytes([len(meta) // 16]) + meta
    parser.feed(chunk)
    titles = [e for e in parser.events() if isinstance(e, TitleChanged)]
    assert titles and titles[0].title == "Don't Stop"
