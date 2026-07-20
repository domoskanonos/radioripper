"""Tests for radio_ripper.services.stream — StreamRecorder with fake HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import FingerprintResult, SavedTrack, TrackInfo
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.fingerprint import FingerprintError, FingerprintProvider
from radio_ripper.services.metadata import NullMetadataProvider
from radio_ripper.services.playlist import StaticPlaylistResolver
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.stream import StreamRecorder, _parse_metaint
from radio_ripper.services.tagging import NullTagger, TrackTagger

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

    async def remove(self, station_name: str, stream_title: str) -> None:
        self.registered = [
            (sn, t) for sn, t in self.registered
            if not (sn == station_name and t.stream_title == stream_title)
        ]

    async def aclose(self) -> None:
        pass

    async def update_fingerprint(
        self, station_name: str, stream_title: str, *,
        recording_id: str, score: float,
    ) -> None:
        pass

    async def exists_by_recording_id(
        self, recording_id: str, exclude_station: str | None = None
    ) -> bool:
        return False

    async def find_by_recording_id(self, recording_id: str) -> None:
        return None

    async def list_untested(self) -> list:
        return []

    async def update_file_path(
        self, station_name: str, stream_title: str, new_path: str
    ) -> None:
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

    async def remove(self, station_name: str, stream_title: str) -> None:
        self.registered = [
            (sn, t) for sn, t in self.registered
            if not (sn == station_name and t.stream_title == stream_title)
        ]

    async def aclose(self) -> None:
        pass

    async def update_fingerprint(
        self, station_name: str, stream_title: str, *,
        recording_id: str, score: float,
    ) -> None:
        pass

    async def exists_by_recording_id(
        self, recording_id: str, exclude_station: str | None = None
    ) -> bool:
        return False

    async def find_by_recording_id(self, recording_id: str) -> None:
        return None

    async def list_untested(self) -> list:
        return []

    async def update_file_path(
        self, station_name: str, stream_title: str, new_path: str
    ) -> None:
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


# ---------------------------------------------------------------------------
# Fingerprint-song tests — directly drive _fingerprint_song without streaming
# ---------------------------------------------------------------------------


class _ScriptedFingerprint(FingerprintProvider):
    """FingerprintProvider stub returning a scripted result or raising."""

    def __init__(
        self,
        *,
        result: FingerprintResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error

    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        if self._error is not None:
            raise self._error
        return self._result


class _RecordingTagger(TrackTagger):
    """TrackTagger stub recording update_acoustid/write_basic calls."""

    def __init__(self) -> None:
        self.update_acoustid_calls: list[tuple[Path, str, float]] = []

    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        pass

    def write_full(
        self,
        file_path: Path,
        artist: str,
        title: str,
        album: str | None = None,
        year: int | None = None,
        cover: bytes | None = None,
    ) -> None:
        pass

    def update_acoustid(self, file_path: Path, recording_id: str, score: float) -> None:
        self.update_acoustid_calls.append((file_path, recording_id, score))


class _FingerprintRepo(TrackRepository):
    """Repo stub recording remove / update_file_path / update_fingerprint."""

    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []
        self.updated_paths: list[tuple[str, str, str]] = []
        self.updated_fps: list[tuple[str, str, str, float]] = []
        self.Exists_by_id_returns = False
        self.Find_by_id_returns: Any = None

    async def exists(self, station_name: str, stream_title: str) -> bool:
        return False

    async def register(self, track: Any, station_name: str) -> None:
        pass

    async def update_enrichment(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def remove(self, station_name: str, stream_title: str) -> None:
        self.removed.append((station_name, stream_title))

    async def aclose(self) -> None:
        pass

    async def update_fingerprint(
        self, station_name: str, stream_title: str, *,
        recording_id: str, score: float,
    ) -> None:
        self.updated_fps.append((station_name, stream_title, recording_id, score))

    async def exists_by_recording_id(
        self, recording_id: str, exclude_station: str | None = None
    ) -> bool:
        return self.Exists_by_id_returns

    async def find_by_recording_id(self, recording_id: str) -> Any:
        return self.Find_by_id_returns

    async def list_untested(self) -> list:
        return []

    async def update_file_path(
        self, station_name: str, stream_title: str, new_path: str
    ) -> None:
        self.updated_paths.append((station_name, stream_title, new_path))


def _make_fp_recorder(
    *,
    settings: Settings,
    repo: _FingerprintRepo,
    tagger: _RecordingTagger,
    fingerprint: FingerprintProvider,
) -> StreamRecorder:
    """A minimal StreamRecorder for fingerprint tests — no HTTP, no playlist."""
    return StreamRecorder(
        station_name="TestStation",
        playlist_url="http://fake.example.com/listen.m3u",
        settings=settings,
        http_client=None,  # type: ignore[arg-type]
        playlist_resolver=StaticPlaylistResolver(["http://fake.example.com/stream"]),
        repository=repo,
        tagger=tagger,
        metadata_provider=NullMetadataProvider(),
        fingerprint_provider=fingerprint,
    )


class TestFingerprintSong:
    """Directly drive StreamRecorder._fingerprint_song() in isolation."""

    async def test_keeps_untested_file_on_fingerprint_error(self, tmp_path) -> None:
        """FingerprintError must NOT delete the .untested.mp3 file."""
        f = tmp_path / "Artist - Title.untested.mp3"
        f.write_bytes(b"\x00")
        settings = _make_settings(tmp_path)
        repo = _FingerprintRepo()
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(error=FingerprintError("API down"))
        rec = _make_fp_recorder(
            settings=settings, repo=repo, tagger=tagger, fingerprint=provider
        )
        track = TrackInfo.from_stream_title("Artist - Title")
        await rec._fingerprint_song(f, track, "prov")
        assert f.exists(), "FingerprintError must keep .untested.mp3"
        assert repo.removed == []
        assert repo.updated_paths == []
        assert tagger.update_acoustid_calls == []

    async def test_discards_file_on_no_match_with_discard_true(self, tmp_path) -> None:
        """None result + discard_unmatched=True → delete file + repo.remove."""
        f = tmp_path / "Artist - Title.untested.mp3"
        f.write_bytes(b"\x00")
        settings = _make_settings(tmp_path)  # discard_unmatched=True (default)
        assert settings.discard_unmatched is True
        repo = _FingerprintRepo()
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(result=None)
        rec = _make_fp_recorder(
            settings=settings, repo=repo, tagger=tagger, fingerprint=provider
        )
        track = TrackInfo.from_stream_title("Artist - Title")
        await rec._fingerprint_song(f, track, "prov")
        assert not f.exists(), "Genuine no-match must delete file"
        assert repo.removed == [("TestStation", "Artist - Title")]
        assert repo.updated_paths == []

    async def test_keeps_file_on_no_match_with_discard_false(self, tmp_path) -> None:
        """None result + discard_unmatched=False → keep file, no repo.remove."""
        f = tmp_path / "Artist - Title.untested.mp3"
        f.write_bytes(b"\x00")
        settings = _make_settings(tmp_path, discard_unmatched=False)
        assert settings.discard_unmatched is False
        repo = _FingerprintRepo()
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(result=None)
        rec = _make_fp_recorder(
            settings=settings, repo=repo, tagger=tagger, fingerprint=provider
        )
        track = TrackInfo.from_stream_title("Artist - Title")
        await rec._fingerprint_song(f, track, "prov")
        assert f.exists(), "discard_unmatched=False must keep the file"
        assert repo.removed == []
        assert repo.updated_paths == []

    async def test_renames_tags_and_updates_db_on_match(self, tmp_path) -> None:
        """FingerprintResult → rename .untested.mp3 → .mp3, tag, DB update."""
        f = tmp_path / "Artist - Title.untested.mp3"
        f.write_bytes(b"\x00")
        settings = _make_settings(tmp_path)
        repo = _FingerprintRepo()
        # Disable cross-station dedup path so the test is focused on rename/tag/update
        repo.Exists_by_id_returns = False
        repo.Find_by_id_returns = None
        tagger = _RecordingTagger()
        result = FingerprintResult(
            artist="Real Artist", title="Real Title", score=0.95, recording_id="rec-42"
        )
        provider = _ScriptedFingerprint(result=result)
        rec = _make_fp_recorder(
            settings=settings, repo=repo, tagger=tagger, fingerprint=provider
        )
        track = TrackInfo.from_stream_title("Artist - Title")
        await rec._fingerprint_song(f, track, "prov")
        expected = tmp_path / "Artist - Title.mp3"
        assert expected.exists(), "Match: file must be renamed to .mp3"
        assert not f.exists(), "Original .untested.mp3 must be gone"
        assert tagger.update_acoustid_calls == [(expected, "rec-42", 0.95)]
        assert repo.updated_paths == [("TestStation", "Artist - Title", str(expected))]
        assert repo.updated_fps == [("TestStation", "Artist - Title", "rec-42", 0.95)]

    async def test_refuses_rename_when_target_mp3_exists(self, tmp_path) -> None:
        """Don't clobber an existing .mp3 — keep .untested.mp3 for manual review."""
        f = tmp_path / "Artist - Title.untested.mp3"
        f.write_bytes(b"\x00")
        # Pre-existing .mp3 must not be overwritten
        existing_mp3 = tmp_path / "Artist - Title.mp3"
        existing_mp3.write_bytes(b"\xff\xfb")
        settings = _make_settings(tmp_path)
        repo = _FingerprintRepo()
        repo.Exists_by_id_returns = False
        repo.Find_by_id_returns = None
        tagger = _RecordingTagger()
        result = FingerprintResult(
            artist="Real Artist", title="Real Title", score=0.95, recording_id="rec-42"
        )
        provider = _ScriptedFingerprint(result=result)
        rec = _make_fp_recorder(
            settings=settings, repo=repo, tagger=tagger, fingerprint=provider
        )
        track = TrackInfo.from_stream_title("Artist - Title")
        await rec._fingerprint_song(f, track, "prov")
        # Both files must still exist
        assert f.exists(), "Refuse-rename: .untested.mp3 must remain"
        assert existing_mp3.exists(), "Refuse-rename: target .mp3 must not be touched"
        # No db update / tag since rename was refused
        assert tagger.update_acoustid_calls == []
        assert repo.updated_paths == []
