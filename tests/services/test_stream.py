"""Tests for radio_ripper.services.stream — StreamRecorder with fake HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.metadata import NullMetadataProvider
from radio_ripper.services.playlist import StaticPlaylistResolver
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.stream import StreamRecorder, _parse_metaint
from radio_ripper.services.tagging import NullTagger

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
        data.extend(b"\x01" * audio_per_song)
        data.extend(_make_meta_block(title))
    return bytes(data)


class FakeHttpClient:
    """Minimal HTTP client mimicking AsyncHttpClient for StreamRecorder tests."""

    def __init__(self, stream_bytes: bytes, metaint: int = METADATA_INTERVAL) -> None:
        self._stream_bytes = stream_bytes
        self._headers = {"icy-metaint": str(metaint)}
        self._last_headers: dict[str, str] = {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        return ""

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        return {}

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        return b""

    async def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[bytes]:
        self._last_headers = dict(self._headers)
        # Stream in small chunks to simulate real streaming
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
    """HTTP client with no icy-metaint header."""

    def __init__(self, stream_bytes: bytes) -> None:
        super().__init__(stream_bytes)
        self._headers = {}


class FakeRepoThatSaysExisting(TrackRepository):
    """TrackRepository where every (station, title) exists — for dup tests."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, SavedTrack]] = []

    async def exists(self, station_name: str, stream_title: str) -> bool:
        return True

    async def register(self, track: SavedTrack, station_name: str) -> None:
        self.registered.append((station_name, track))

    async def update_enrichment(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def aclose(self) -> None:
        pass


class FakeRepoFresh(TrackRepository):
    """TrackRepository that never says exists — for recording tests."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, SavedTrack]] = []

    async def exists(self, station_name: str, stream_title: str) -> bool:
        return False

    async def register(self, track: SavedTrack, station_name: str) -> None:
        self.registered.append((station_name, track))

    async def update_enrichment(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def aclose(self) -> None:
        pass


class FakeRepoExistingAfterFirst(FakeRepoFresh):
    """Repo that says exists after first register (dup-loop test)."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()

    async def exists(self, station_name: str, stream_title: str) -> bool:
        return stream_title in self._seen

    async def register(self, track: SavedTrack, station_name: str) -> None:
        self._seen.add(track.stream_title)
        super().register(track, station_name)


def _make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "destination": tmp_path / "recordings",
        "database": tmp_path / "ripper.db",
        "streams": [StreamConfig(name="TestStation", url="http://fake.example.com/listen.m3u")],
        "reconnect_base_delay": 0.1,
        "reconnect_max_delay": 1.0,
        "min_file_size_bytes": 10,
        "enrich_metadata": False,
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _make_recorder(
    *,
    settings: Settings,
    http_client: Any,
    repo: TrackRepository,
    destination: Any,
) -> StreamRecorder:
    return StreamRecorder(
        station_name="TestStation",
        playlist_url="http://fake.example.com/listen.m3u",
        settings=settings,
        http_client=http_client,
        playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
        repository=repo,
        tagger=NullTagger(),
        metadata_provider=NullMetadataProvider(),
        enrich_semaphore=asyncio.Semaphore(1),
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
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        # Two songs should have been recorded + registered (the first title
        # is "Already Playing" and gets discarded; "Artist A" and "Artist B"
        # are recorded until the next boundary — but "Artist B" only completes
        # if we reach its end, which may not happen in this buffer.
        # At minimum, "Artist A - Song A" should be recorded and registered.
        titles = [t.stream_title for _, t in repo.registered]
        assert "Artist A - Song A" in titles

    async def test_discards_first_song_on_join(self, tmp_path):
        """The first running song at join time is discarded."""
        stream = _make_stream_bytes(
            ["Mid Song", "Real Artist - Real Title", "Other - Other"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        titles = [t.stream_title for _, t in repo.registered]
        assert "Mid Song" not in titles

    async def test_skips_duplicate(self, tmp_path):
        """If the repo says the song already exists, it is not recorded."""
        stream = _make_stream_bytes(
            ["Joining Song", "Dub - Dup", "Real - Real"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoThatSaysExisting()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        # Nothing registered because exists() always returns True
        assert repo.registered == []

    async def test_no_metaint_returns_false(self, tmp_path):
        """Stream without icy-metaint header returns False (reconnect)."""
        stream = _make_stream_bytes(["A - B"])
        client = FakeHttpClientNoMeta(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        task = asyncio.create_task(rec._run_forever())
        await asyncio.sleep(0.3)
        rec.stop()
        await asyncio.wait_for(task, timeout=3)
        assert repo.registered == []

    async def test_stop_event_stops_recorder(self, tmp_path):
        """Recorder respects stop() and exits gracefully."""
        stream = _make_stream_bytes(["A - B"] * 100)
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        rec.start()
        await asyncio.sleep(0.1)
        rec.stop()
        await asyncio.wait_for(rec.join(), timeout=5)

    async def test_empty_playlist_returns_false(self, tmp_path):
        """Empty playlist results in a failed run_once (reconnect)."""
        from radio_ripper.services.playlist import StaticPlaylistResolver

        stream = b""
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver([]),
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
        )
        ok = await rec._run_once()
        assert ok is False

    async def test_file_written_to_disk(self, tmp_path):
        """A recorded song ends up as an .mp3 file on disk."""
        stream = _make_stream_bytes(
            ["Mid", "Adele - Hello", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path, min_file_size_bytes=1)
        repo = FakeRepoFresh()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        files = list(settings.destination.rglob("*.mp3"))
        assert len(files) >= 1
        assert any("Adele" in f.name for f in files)


class TestAdTitlePatterns:
    async def test_ad_title_is_not_recorded(self, tmp_path):
        """Titles matching ad_title_patterns are skipped entirely."""
        stream = _make_stream_bytes(
            ["Joining", "Werbung - Spot", "Artist - Real Song", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            ad_title_patterns=["^Werbung"],
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        titles = [t.stream_title for _, t in repo.registered]
        assert "Werbung - Spot" not in titles
        assert "Artist - Real Song" in titles

    async def test_ad_pattern_case_insensitive(self, tmp_path):
        """Ad pattern matching is case-insensitive."""
        stream = _make_stream_bytes(
            ["Joining", "ADVERTISEMENT", "Artist - Song", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            ad_title_patterns=["advertisement"],
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        titles = [t.stream_title for _, t in repo.registered]
        assert "ADVERTISEMENT" not in titles
        assert "Artist - Song" in titles

    async def test_no_patterns_records_everything(self, tmp_path):
        """Without patterns, all non-empty titles are recorded normally."""
        stream = _make_stream_bytes(
            ["Joining", "Werbung", "Artist - Song", "Next - Song"],
            audio_per_song=METADATA_INTERVAL,
        )
        client = FakeHttpClient(stream)
        settings = _make_settings(tmp_path)
        repo = FakeRepoFresh()
        rec = _make_recorder(
            settings=settings, http_client=client, repo=repo, destination=settings.destination
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        titles = [t.stream_title for _, t in repo.registered]
        # Without patterns, "Werbung" is treated as a normal title
        assert "Werbung" in titles


class TestPreBufferBytes:
    async def test_pre_buffer_skips_first_bytes(self, tmp_path):
        """pre_buffer_bytes causes the first N bytes of each recording to be dropped."""
        audio_per_song = 200
        stream = _make_stream_bytes(
            ["Joining", "Artist - Song", "Next - Song"],
            audio_per_song=audio_per_song,
        )
        client = FakeHttpClient(stream, metaint=audio_per_song)
        settings = _make_settings(tmp_path, min_file_size_bytes=1)
        repo = FakeRepoFresh()
        skip = 50
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            pre_buffer_bytes=skip,
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        titles = [t.stream_title for _, t in repo.registered]
        assert "Artist - Song" in titles
        # File should be smaller than the full audio_per_song because of the skip
        files = list(settings.destination.rglob("*.mp3"))
        song_file = next((f for f in files if "Artist" in f.name), None)
        assert song_file is not None
        assert song_file.stat().st_size < audio_per_song

    async def test_zero_pre_buffer_writes_all_bytes(self, tmp_path):
        """pre_buffer_bytes=0 (default) writes all audio bytes unchanged."""
        audio_per_song = 200
        stream = _make_stream_bytes(
            ["Joining", "Artist - Song", "Next - Song"],
            audio_per_song=audio_per_song,
        )
        client = FakeHttpClient(stream, metaint=audio_per_song)
        settings = _make_settings(tmp_path, min_file_size_bytes=1)
        repo = FakeRepoFresh()
        rec = StreamRecorder(
            station_name="TestStation",
            playlist_url="http://fake.example.com/listen.m3u",
            settings=settings,
            http_client=client,
            playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            pre_buffer_bytes=0,
        )
        task = rec.start()
        await asyncio.sleep(0.5)
        rec.stop()
        await asyncio.wait_for(task, timeout=5)
        files = list(settings.destination.rglob("*.mp3"))
        song_file = next((f for f in files if "Artist" in f.name), None)
        assert song_file is not None
        assert song_file.stat().st_size >= audio_per_song
