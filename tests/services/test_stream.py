"""Tests for radio_ripper.services.stream — StreamRecorder with fake HTTP."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from radio_ripper.infra.config import Settings
from radio_ripper.services.playlist import StaticPlaylistResolver
from radio_ripper.services.stream import StreamRecorder, _parse_metaint

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

METADATA_INTERVAL = 100  # bytes of audio between metadata blocks


def _make_meta_block(stream_title: str) -> bytes:
    payload = f"StreamTitle='{stream_title}';".encode()
    padding = (16 - (len(payload) % 16)) % 16
    payload += b"\x00" * padding
    length_byte = len(payload) // 16
    return bytes([length_byte]) + payload


def _make_stream_bytes(titles: list[str], audio_per_song: int = METADATA_INTERVAL) -> bytes:
    data = bytearray()
    for title in titles:
        data.extend(b"\xff\xfb\x01" + b"\x01" * (audio_per_song - 3))
        data.extend(_make_meta_block(title))
    return bytes(data)


class FakeHttpClient:
    def __init__(self, stream_bytes: bytes, metaint: int = METADATA_INTERVAL) -> None:
        self._stream_bytes = stream_bytes
        self._headers = {"icy-metaint": str(metaint)}
        self._last_headers: dict[str, str] = {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        return ""

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        return {}

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        return b""

    async def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[bytes, None]:
        self._last_headers = dict(self._headers)
        chunk_size = 64
        for i in range(0, len(self._stream_bytes), chunk_size):
            chunk = self._stream_bytes[i : i + chunk_size]
            yield chunk
            await asyncio.sleep(0)

    def response_headers(self) -> dict[str, str]:
        return dict(self._last_headers)

    async def aclose(self) -> None:
        pass


class FakeHttpClientNoMeta(FakeHttpClient):
    def __init__(self, stream_bytes: bytes) -> None:
        super().__init__(stream_bytes)
        self._headers = {}


def _make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "work_dir": str(tmp_path / "work"),
        "mp3_inbox": str(tmp_path / "work" / "mp3_inbox"),
        "min_file_size_bytes": 1,
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _make_recorder(
    *,
    settings: Settings,
    http_client: Any,
    logger: Any = None,
    ignore_title_patterns: Any = None,
    no_icy_disable_after: int = 10,
) -> StreamRecorder:
    return StreamRecorder(
        station_name="TestStation",
        playlist_url="http://fake.example.com/listen.m3u",
        settings=settings,
        http_client=http_client,
        playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
        logger=logger,
        ignore_title_patterns=ignore_title_patterns,
        no_icy_disable_after=no_icy_disable_after,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParseMetaint:
    def test_standard(self):
        assert _parse_metaint({"icy-metaint": "16000"}) == 16000

    def test_case_variants(self):
        assert _parse_metaint({"Icy-Metaint": "8000"}) == 8000
        assert _parse_metaint({"ICY-METAINT": "4000"}) == 4000

    def test_missing(self):
        assert _parse_metaint({}) is None

    def test_invalid_value(self):
        assert _parse_metaint({"icy-metaint": "not-a-number"}) is None


class TestStreamRecorder:
    async def test_records_complete_song(self, tmp_path):
        """Song that runs from one title boundary to the next is saved."""
        stream = _make_stream_bytes(
            ["Already Playing Song", "Artist A - Song A", "Artist B - Song B"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path, min_file_size_bytes=1)
        rec = _make_recorder(settings=settings, http_client=client)
        stream_dir = settings.work_dir / "mp3_inbox"
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert any("Artist A - Song A" in f for f in files)

    async def test_discards_first_song_on_join(self, tmp_path):
        """The first running song at join time is discarded."""
        stream = _make_stream_bytes(
            ["Mid Song", "Real Artist - Real Title", "Other - Other"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        rec = _make_recorder(settings=settings, http_client=client)
        stream_dir = settings.work_dir / "mp3_inbox"
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert not any("Mid Song" in f for f in files)

    async def test_no_metaint_returns_false(self, tmp_path):
        """Stream without icy-metaint header returns False (reconnect)."""
        stream = _make_stream_bytes(["A - B"])
        client = FakeHttpClientNoMeta(stream)
        settings = _make_settings(tmp_path)
        rec = _make_recorder(settings=settings, http_client=client)
        task = asyncio.create_task(rec._run_forever())
        await asyncio.sleep(0.3)
        rec.stop()
        await asyncio.wait_for(task, timeout=3)
        stream_dir = settings.work_dir / "mp3_inbox"
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert not any("A - B" in f for f in files)

    async def test_stop_event_stops_recorder(self, tmp_path):
        """Recorder respects stop() and exits gracefully."""
        stream = _make_stream_bytes(["A - B"] * 100)
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        rec = _make_recorder(settings=settings, http_client=client)
        rec.start()
        await asyncio.sleep(0.1)
        rec.stop()
        await asyncio.wait_for(rec.join(), timeout=5)

    async def test_empty_playlist_returns_false(self, tmp_path):
        """Empty playlist results in a failed run_once (reconnect)."""
        stream = b""
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver([]),
        )
        ok = await rec._run_once()
        assert ok is False

    async def test_file_written_to_disk(self, tmp_path):
        """A recorded song ends up as an .mp3 file in mp3_inbox."""
        stream = _make_stream_bytes(
            ["Mid", "Adele - Hello", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path, min_file_size_bytes=1)
        rec = _make_recorder(settings=settings, http_client=client)
        stream_dir = settings.work_dir / "mp3_inbox"
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        files = list(stream_dir.glob("*.mp3")) if stream_dir.is_dir() else []
        assert len(files) >= 1
        assert any("Adele" in f.name for f in files)


class TestIgnorePatterns:
    async def test_ignored_title_is_not_recorded(self, tmp_path):
        """Titles matching ignore_title_patterns are skipped entirely."""
        stream = _make_stream_bytes(
            ["Joining", "Werbung - Spot", "Artist - Real Song", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        import shutil

        stream_dir = settings.work_dir / "mp3_inbox"
        if stream_dir.is_dir():
            shutil.rmtree(stream_dir)
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            ignore_title_patterns=["^Werbung"],
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        stream_dir = settings.work_dir / "mp3_inbox"
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert not any("Werbung" in f for f in files)
        assert any("Artist" in f for f in files)

    async def test_ignore_pattern_case_insensitive(self, tmp_path):
        """Ignore pattern matching is case-insensitive."""
        stream = _make_stream_bytes(
            ["Joining", "ADVERTISEMENT", "Artist - Song", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            ignore_title_patterns=["advertisement"],
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        stream_dir = settings.work_dir / "mp3_inbox"
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert not any("ADVERTISEMENT" in f for f in files)
        assert any("Artist" in f for f in files)

    async def test_no_patterns_records_everything(self, tmp_path):
        """Without patterns, all non-empty titles are recorded normally."""
        stream = _make_stream_bytes(
            ["Joining", "Werbung", "Artist - Song", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        rec = _make_recorder(settings=settings, http_client=client)
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        stream_dir = settings.work_dir / "mp3_inbox"
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert any("Werbung" in f for f in files)


class _FailingResolver:
    async def resolve(self, url: str) -> list[str]:
        raise RuntimeError("network error")


class _HttpClientStreamingError:
    def __init__(self, stream_bytes: bytes) -> None:
        self._stream_bytes = stream_bytes

    async def stream_binary(self, url: str, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        raise OSError("connection refused")

    def response_headers(self) -> dict[str, str]:
        return {}


class TestRunForeverExceptions:
    async def test_uncaught_error_in_run_once(self, tmp_path: Path, caplog: Any) -> None:
        """A generic exception from the playlist resolver is caught."""
        dest = tmp_path / "recordings"
        dest.mkdir()
        src = _make_stream_bytes(["Song A"])
        client = FakeHttpClient(src)
        settings = _make_settings(tmp_path, reconnect_base_delay=0.1, reconnect_max_delay=1.0)
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=_FailingResolver(),
        )
        caplog.set_level(logging.DEBUG, logger="radio_ripper.stream")
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        assert "Uncaught error in recorder 'TestStation'" in caplog.text

    async def test_disabled_after_no_icy_limit(self, tmp_path: Path, caplog: Any) -> None:
        """Recorder disables itself after no_icy_disable_after consecutive
        streams without ICY metadata."""
        dest = tmp_path / "recordings"
        dest.mkdir()
        stream = _make_stream_bytes(["Does not matter"])
        client = FakeHttpClientNoMeta(stream)
        settings = _make_settings(tmp_path, no_icy_disable_after=1)
        rec = _make_recorder(
            settings=settings,
            http_client=client,
            no_icy_disable_after=1,
        )
        caplog.set_level(logging.DEBUG, logger="radio_ripper.stream")
        task = rec.start()
        await asyncio.wait_for(task, timeout=10)
        assert "no ICY metadata after 1 consecutive attempts" in caplog.text
        assert not rec._stop_event.is_set()

    async def test_disabled_after_too_many_connect_failures(self, tmp_path: Path, caplog: Any) -> None:
        """Recorder disables after no_icy_disable_after connect failures."""
        dest = tmp_path / "recordings"
        dest.mkdir()
        settings = _make_settings(tmp_path)
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=_HttpClientStreamingError(b""),
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            no_icy_disable_after=1,
        )
        caplog.set_level(logging.DEBUG, logger="radio_ripper.stream")
        task = rec.start()
        await asyncio.wait_for(task, timeout=10)
        assert "connect failed" in caplog.text
        assert "connect failed 1 times in a row" in caplog.text


class TestBlankOrAdTitles:
    async def test_blank_title_skips_recording(self, tmp_path: Path) -> None:
        """A stream title that contains only whitespace is skipped."""
        stream = _make_stream_bytes(["Joining", "   ", "Artist - Real Song", "Another - Song"])
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        rec = _make_recorder(settings=settings, http_client=client)
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        stream_dir = settings.work_dir / "mp3_inbox"
        files = [f.name for f in stream_dir.glob("*.mp3")] if stream_dir.is_dir() else []
        assert all("   " not in f for f in files)
        assert any("Artist" in f for f in files)


class TestDiscardSmallFile:
    async def test_discards_when_below_min_file_size(self, tmp_path: Path) -> None:
        """A song with less audio than min_file_size_bytes is discarded."""
        import shutil

        stream_dir = tmp_path / "work" / "mp3_inbox"
        if stream_dir.is_dir():
            shutil.rmtree(stream_dir)
        stream = _make_stream_bytes(["Joining", "Artist - Too Small", "Next - Song"])
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path, min_file_size_bytes=250)
        rec = _make_recorder(settings=settings, http_client=client)
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            pass
        files = list(stream_dir.glob("*.mp3")) if stream_dir.is_dir() else []
        assert not any("Too Small" in f.name for f in files)
