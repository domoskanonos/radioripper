"""Tests for radio_ripper.app — RadioRipperApp composition."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from radio_ripper.app import RadioRipperApp
from radio_ripper.domain.models import FingerprintResult, SavedTrack
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintError,
    FingerprintProvider,
    NullFingerprintProvider,
)
from radio_ripper.services.metadata import NullMetadataProvider
from radio_ripper.services.playlist import StaticPlaylistResolver
from radio_ripper.services.repository import TrackRecord, TrackRepository
from radio_ripper.services.tagging import NullTagger, TrackTagger


def _make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "destination": tmp_path / "recordings",
        "database": tmp_path / "ripper.db",
        "streams": [StreamConfig(name="TestStation", url="http://fake.example.com/listen.m3u")],
        "enrich_metadata": False,
        "enrichment_workers": 2,
    }
    base.update(overrides)
    return Settings.model_validate(base)


class FakeRepo(TrackRepository):
    """Minimal in-memory repo stub for app tests."""

    async def exists(self, station_name: str, stream_title: str) -> bool:
        return False

    async def register(self, track: Any, station_name: str) -> None:
        pass

    async def update_enrichment(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def remove(self, station_name: str, stream_title: str) -> None:
        pass

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


class TestRadioRipperApp:
    async def test_create_recorders_for_each_stream(self, tmp_path):
        settings = _make_settings(tmp_path)
        client = AsyncMock()
        client.aclose = AsyncMock()
        repo = FakeRepo()
        tagger = NullTagger()
        metadata = NullMetadataProvider()
        resolver = StaticPlaylistResolver(["http://x"])

        app = RadioRipperApp(
            settings=settings,
            client=client,
            repository=repo,
            tagger=tagger,
            metadata_provider=metadata,
            playlist_resolver=resolver,
        )
        assert len(app.recorders()) == 0
        await app.start()
        assert len(app.recorders()) == 1
        await app.stop()

    async def test_stop_closes_client_and_repo(self, tmp_path):
        settings = _make_settings(tmp_path)
        client = AsyncMock()
        client.aclose = AsyncMock()
        repo = MagicMock(spec=TrackRepository)
        repo.aclose = AsyncMock()

        app = RadioRipperApp(
            settings=settings,
            client=client,
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        await app.stop()
        client.aclose.assert_called_once()
        repo.aclose.assert_awaited_once()

    async def test_multiple_streams(self, tmp_path):
        settings = Settings.model_validate(
            {
                "destination": str(tmp_path / "recordings"),
                "database": str(tmp_path / "ripper.db"),
                "streams": [
                    {"name": "Station1", "url": "http://example.com/1.m3u"},
                    {"name": "Station2", "url": "http://example.com/2.m3u"},
                    {"name": "Station3", "url": "http://example.com/3.m3u"},
                ],
                "enrich_metadata": False,
            }
        )
        client = AsyncMock()
        client.aclose = AsyncMock()
        repo = FakeRepo()

        app = RadioRipperApp(
            settings=settings,
            client=client,
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        assert len(app.recorders()) == 3
        await app.stop()

    async def test_no_streams_logs_error(self, tmp_path):
        settings = Settings.model_validate(
            {
                "destination": str(tmp_path / "recordings"),
                "database": str(tmp_path / "ripper.db"),
                "streams": [{"name": "S1", "url": "http://example.com/1.m3u"}],
                "enrich_metadata": False,
            }
        )
        # Empty streams list — need to use model_validate with override
        from radio_ripper.infra.config import Settings as S

        settings = S.model_validate(
            {
                "destination": str(tmp_path / "recordings"),
                "database": str(tmp_path / "ripper.db"),
                "streams": [{"name": "S1", "url": "http://example.com/1.m3u"}],
                "enrich_metadata": False,
            }
        )
        client = AsyncMock()
        repo = FakeRepo()

        app = RadioRipperApp(
            settings=settings,
            client=client,
            repository=repo,
            tagger=NullTagger(),
            metadata_provider=NullMetadataProvider(),
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        assert len(app.recorders()) == 1
        await app.stop()


# ---------------------------------------------------------------------------
# Stubs for reprocess_untested tests
# ---------------------------------------------------------------------------


class _StubRepo(TrackRepository):
    """Repo stub recording remove / update_file_path / update_fingerprint calls."""

    def __init__(self, *, untested: list[TrackRecord] | None = None) -> None:
        self.untested: list[TrackRecord] = untested or []
        self.removed: list[tuple[str, str]] = []
        self.updated_paths: list[tuple[str, str, str]] = []
        self.updated_fps: list[tuple[str, str, str, float]] = []

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
        return False

    async def find_by_recording_id(self, recording_id: str) -> Any:
        return None

    async def list_untested(self) -> list[TrackRecord]:
        return list(self.untested)

    async def update_file_path(
        self, station_name: str, stream_title: str, new_path: str
    ) -> None:
        self.updated_paths.append((station_name, stream_title, new_path))


class _ScriptedFingerprint(FingerprintProvider):
    """FingerprintProvider stub returning different results per call or raising."""

    def __init__(
        self,
        *,
        results: list[FingerprintResult | None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._results = list(results) if results is not None else []
        self._error = error
        self.call_count = 0
        self.call_times: list[float] = []

    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        self.call_count += 1
        self.call_times.append(time.monotonic())
        if self._error is not None:
            raise self._error
        if self._results:
            return self._results.pop(0)
        return None


class _RecordingTagger(TrackTagger):
    """TrackTagger stub recording update_acoustid calls."""

    def __init__(self) -> None:
        self.update_acoustid_calls: list[tuple[Path, str, float]] = []

    def write_basic(self, file_path: Path, track: Any, provenance: str) -> None:
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

    def embed_cover(self, file_path: Path, cover_bytes: bytes) -> None:
        pass


def _untested_record(
    tmp_path: Path, name: str = "Artist - Title"
) -> tuple[TrackRecord, Path]:
    """Create a real .untested.mp3 on disk and return (record, path)."""
    f = tmp_path / f"{name}.untested.mp3"
    f.write_bytes(b"\x00" * 32)
    rec = TrackRecord(
        station_name="TestStation",
        track=SavedTrack(
            stream_title=name,
            artist="Artist",
            title="Title",
            file_path=str(f),
            file_size=32,
        ),
    )
    return rec, f


def _make_app(
    settings: Settings,
    repo: _StubRepo,
    tagger: TrackTagger,
    fingerprint: FingerprintProvider,
) -> RadioRipperApp:
    client = AsyncMock()
    client.aclose = AsyncMock()
    return RadioRipperApp(
        settings=settings,
        client=client,
        repository=repo,
        tagger=tagger,
        metadata_provider=NullMetadataProvider(),
        fingerprint_provider=fingerprint,
        playlist_resolver=StaticPlaylistResolver(["http://x"]),
    )


class TestReprocessUntested:
    """RadioRipperApp.reprocess_untested() — runs at start() time."""

    async def test_skips_when_no_acoustid_provider(self, tmp_path) -> None:
        """NullFingerprintProvider → no list_untested call at all."""
        settings = _make_settings(tmp_path)
        repo = _StubRepo()
        app = _make_app(
            settings, repo, NullTagger(), NullFingerprintProvider()
        )
        await app.reprocess_untested()
        assert repo.removed == []
        assert repo.updated_paths == []
        assert repo.updated_fps == []

    async def test_noop_when_no_untested_records(self, tmp_path) -> None:
        """list_untested=[] → no fingerprint calls, no removals."""
        settings = _make_settings(tmp_path, acoustid_min_interval_s=0.0)
        repo = _StubRepo(untested=[])
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(error=RuntimeError("should not happen"))
        app = _make_app(settings, repo, tagger, provider)
        await app.reprocess_untested()
        assert provider.call_count == 0
        assert repo.removed == []
        assert repo.updated_paths == []

    async def test_keeps_file_on_fingerprint_error(self, tmp_path) -> None:
        """FingerprintError → file remains, no DB mutations."""
        settings = _make_settings(tmp_path, acoustid_min_interval_s=0.0)
        rec, f = _untested_record(tmp_path)
        repo = _StubRepo(untested=[rec])
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(error=FingerprintError("API down"))
        app = _make_app(settings, repo, tagger, provider)
        await app.reprocess_untested()
        assert f.exists(), "On FingerprintError, .untested.mp3 must remain"
        assert repo.removed == []
        assert repo.updated_paths == []
        assert repo.updated_fps == []
        assert tagger.update_acoustid_calls == []

    async def test_discards_file_on_no_match(self, tmp_path) -> None:
        """None + discard_unmatched=True → file deleted, repo.remove called."""
        settings = _make_settings(tmp_path, acoustid_min_interval_s=0.0)
        rec, f = _untested_record(tmp_path)
        repo = _StubRepo(untested=[rec])
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(results=[None])
        app = _make_app(settings, repo, tagger, provider)
        await app.reprocess_untested()
        assert not f.exists(), "No-match must delete file when discard_unmatched=True"
        assert repo.removed == [("TestStation", "Artist - Title")]
        assert repo.updated_paths == []

    async def test_refuses_rename_when_target_mp3_exists(self, tmp_path) -> None:
        """When target .mp3 already exists, .untested.mp3 must be kept untouched."""
        settings = _make_settings(tmp_path, acoustid_min_interval_s=0.0)
        rec, f = _untested_record(tmp_path)
        existing_mp3 = tmp_path / "Artist - Title.mp3"
        existing_mp3.write_bytes(b"\xff\xfb")
        repo = _StubRepo(untested=[rec])
        tagger = _RecordingTagger()
        result = FingerprintResult(
            artist="Real Artist", title="Real Title", score=0.95, recording_id="rec-42"
        )
        provider = _ScriptedFingerprint(results=[result])
        app = _make_app(settings, repo, tagger, provider)
        await app.reprocess_untested()
        assert f.exists(), "Refuse-rename: .untested.mp3 must still exist"
        assert existing_mp3.exists(), "Refuse-rename: target .mp3 must not be clobbered"
        assert repo.updated_paths == []
        assert repo.updated_fps == []
        assert tagger.update_acoustid_calls == []

    async def test_renames_and_updates_db_on_match(self, tmp_path) -> None:
        """Happy path: rename + tag + DB updates."""
        settings = _make_settings(tmp_path, acoustid_min_interval_s=0.0)
        rec, f = _untested_record(tmp_path)
        repo = _StubRepo(untested=[rec])
        tagger = _RecordingTagger()
        result = FingerprintResult(
            artist="Real Artist", title="Real Title", score=0.95, recording_id="rec-42"
        )
        provider = _ScriptedFingerprint(results=[result])
        app = _make_app(settings, repo, tagger, provider)
        await app.reprocess_untested()
        expected = tmp_path / "Artist - Title.mp3"
        assert expected.exists(), "Happy path: .mp3 must exist after rename"
        assert not f.exists(), "Happy path: .untested.mp3 must be gone"
        assert tagger.update_acoustid_calls == [(expected, "rec-42", 0.95)]
        assert repo.updated_paths == [
            ("TestStation", "Artist - Title", str(expected))
        ]
        assert repo.updated_fps == [
            ("TestStation", "Artist - Title", "rec-42", 0.95)
        ]

    async def test_respects_rate_limit_interval(self, tmp_path) -> None:
        """With acoustid_min_interval_s=0.05, calls must be >=0.05s apart."""
        settings = _make_settings(tmp_path, acoustid_min_interval_s=0.05)
        records: list[TrackRecord] = []
        for i in range(3):
            r, _ = _untested_record(tmp_path, name=f"Artist - Title {i}")
            records.append(r)
        repo = _StubRepo(untested=records)
        tagger = _RecordingTagger()
        provider = _ScriptedFingerprint(results=[None, None, None])
        app = _make_app(settings, repo, tagger, provider)
        await app.reprocess_untested()
        assert provider.call_count == 3
        deltas = [
            provider.call_times[i + 1] - provider.call_times[i]
            for i in range(len(provider.call_times) - 1)
        ]
        for d in deltas:
            assert d >= 0.05 - 0.01, f"Rate-limit violated: delta={d:.3f}s < 0.05s"
