"""Tests for radio_ripper.app — RadioRipperApp composition."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from radio_ripper.app import RadioRipperApp
from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.fingerprint import (
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
        self,
        station_name: str,
        stream_title: str,
        *,
        recording_id: str,
        score: float,
    ) -> None:
        pass

    async def find_all_by_recording_id(self, recording_id: str) -> list[TrackRecord]:
        return []

    async def find_all_by_artist_title(self, artist: str, title: str) -> list[TrackRecord]:
        return []

    async def list_all(self) -> list[TrackRecord]:
        return []

    async def find_by_file_path(self, file_path: str) -> None:
        return None

    async def update_file_path(self, station_name: str, stream_title: str, new_path: str) -> None:
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


def _make_app(
    settings: Settings,
    repo: TrackRepository,
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


class _LookupStubRepo(TrackRepository):
    """Repo stub that returns records by file_path."""

    def __init__(self, records: dict[str, TrackRecord]) -> None:
        self.records = records
        self.updated_paths: list[tuple[str, str, str]] = []

    async def exists(self, *args, **kwargs) -> bool:
        return False

    async def register(self, *args, **kwargs) -> None:
        pass

    async def update_enrichment(self, *args, **kwargs) -> None:
        pass

    async def remove(self, *args, **kwargs) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def update_fingerprint(self, **kwargs) -> None:
        pass

    async def list_all(self) -> list[TrackRecord]:
        return list(self.records.values())

    async def find_all_by_recording_id(self, recording_id: str) -> list[TrackRecord]:
        return []

    async def find_all_by_artist_title(self, artist: str, title: str) -> list[TrackRecord]:
        return []

    async def find_by_file_path(self, file_path: str) -> TrackRecord | None:
        return self.records.get(file_path)

    async def update_file_path(self, station_name: str, stream_title: str, new_path: str) -> None:
        self.updated_paths.append((station_name, stream_title, new_path))


class TestReprocessAll:
    """RadioRipperApp._reprocess_all() — reset .mp3 → .untested.mp3."""

    async def test_renames_and_updates_db(self, tmp_path) -> None:
        dest = tmp_path / "recordings"
        mp3_file = dest / "Artist - Title.mp3"
        mp3_file.parent.mkdir(parents=True)
        mp3_file.write_bytes(b"\xff\xfb" + b"\x00" * 100)
        record = TrackRecord(
            station_name="TestStation",
            track=SavedTrack(
                stream_title="Artist - Title",
                artist="Artist",
                title="Title",
                file_path=str(mp3_file),
                file_size=102,
            ),
        )
        repo = _LookupStubRepo(records={str(mp3_file): record})
        settings = _make_settings(tmp_path, reprocess_all=True)
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._reprocess_all()
        restructured = dest / "Artist" / "Artist - Title.mp3"
        assert restructured.exists(), "File must be restructured into artist folder"
        assert not mp3_file.exists(), "Original .mp3 must be gone"
        assert repo.updated_paths == [("TestStation", "Artist - Title", str(restructured))]

    async def test_skips_untested_files(self, tmp_path) -> None:
        dest = tmp_path / "recordings"
        untested = dest / "Artist - Title.untested.mp3"
        untested.parent.mkdir(parents=True)
        untested.write_bytes(b"\x00" * 32)
        repo = _LookupStubRepo(records={})
        settings = _make_settings(tmp_path, reprocess_all=True)
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._reprocess_all()
        assert untested.exists(), ".untested.mp3 must not be touched"
        assert repo.updated_paths == []

    async def test_skips_orphan_files_without_db_entry(self, tmp_path) -> None:
        dest = tmp_path / "recordings"
        mp3_file = dest / "Orphan - File.mp3"
        mp3_file.parent.mkdir(parents=True)
        mp3_file.write_bytes(b"\x00" * 32)
        repo = _LookupStubRepo(records={})
        settings = _make_settings(tmp_path, reprocess_all=True)
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._reprocess_all()
        assert mp3_file.exists(), "Orphan .mp3 without DB entry must not be renamed"
        assert repo.updated_paths == []

    async def test_noop_when_disabled(self, tmp_path) -> None:
        dest = tmp_path / "recordings"
        mp3_file = dest / "Artist - Title.mp3"
        mp3_file.parent.mkdir(parents=True)
        mp3_file.write_bytes(b"\x00" * 32)
        record = TrackRecord(
            station_name="TestStation",
            track=SavedTrack(
                stream_title="Artist - Title",
                artist="Artist",
                title="Title",
                file_path=str(mp3_file),
                file_size=32,
            ),
        )
        repo = _LookupStubRepo(records={str(mp3_file): record})
        settings = _make_settings(tmp_path, reprocess_all=False)
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._reprocess_all()
        assert mp3_file.exists(), ".mp3 must stay untouched when reprocess_all=False"
        assert repo.updated_paths == []



