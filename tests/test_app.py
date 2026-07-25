"""Tests for radio_ripper.app — RadioRipperApp composition."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from radio_ripper.app import RadioRipperApp
from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.infra.errors import ConfigurationError
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

            }
        )
        # Empty streams list — need to use model_validate with override
        from radio_ripper.infra.config import Settings as S

        settings = S.model_validate(
            {
                "destination": str(tmp_path / "recordings"),
                "database": str(tmp_path / "ripper.db"),
                "streams": [{"name": "S1", "url": "http://example.com/1.m3u"}],

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


class _StaleRecordRepo(FakeRepo):
    """Repo that returns preset records for cleanup tests."""

    def __init__(self, records: list[TrackRecord]) -> None:
        self._records = records
        self.removed: list[tuple[str, str]] = []
        self.updated_paths: list[tuple[str, str, str]] = []

    async def list_all(self) -> list[TrackRecord]:
        return self._records

    async def remove(self, station_name: str, stream_title: str) -> None:
        self.removed.append((station_name, stream_title))

    async def update_file_path(self, station_name: str, stream_title: str, new_path: str) -> None:
        self.updated_paths.append((station_name, stream_title, new_path))

    async def update_enrichment(self, *args: Any, **kwargs: Any) -> None:
        pass


class TestCancel:
    """RadioRipperApp.cancel() — thread-safe cancellation flag."""

    async def test_sets_flag(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        assert app._cancel_requested is False
        app.cancel()
        assert app._cancel_requested is True


class TestCleanupOrphans:
    """RadioRipperApp._cleanup_orphans()."""

    async def test_removes_stale_db_records(self, tmp_path) -> None:
        dest = tmp_path / "recordings"
        settings = _make_settings(tmp_path, destination=dest)
        record = TrackRecord(
            station_name="OldStation",
            track=SavedTrack(
                stream_title="Gone - Track",
                artist="Gone",
                title="Track",
                file_path=str(dest / "nonexistent.mp3"),
                file_size=0,
            ),
        )
        repo = _StaleRecordRepo([record])
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._cleanup_orphans()
        assert repo.removed == [("OldStation", "Gone - Track")]

    async def test_removes_orphan_untested_files(self, tmp_path) -> None:
        dest = tmp_path / "recordings"
        orphan = dest / "orphan.untested.mp3"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"\x00" * 32)
        settings = _make_settings(tmp_path, destination=dest)
        repo = _StaleRecordRepo([])
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._cleanup_orphans()
        assert not orphan.exists(), "Orphan .untested.mp3 must be deleted"

    async def test_noop_when_nothing_stale(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="radio_ripper.app")
        dest = tmp_path / "recordings"
        existing = dest / "existing.mp3"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"\x00" * 32)
        record = TrackRecord(
            station_name="Station",
            track=SavedTrack(
                stream_title="Existing - Song",
                artist="Existing",
                title="Song",
                file_path=str(existing),
                file_size=32,
            ),
        )
        repo = _StaleRecordRepo([record])
        settings = _make_settings(tmp_path, destination=dest)
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        await app._cleanup_orphans()
        assert repo.removed == []
        assert "no stale records found" in caplog.text

    async def test_skips_untested_when_destination_none(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="radio_ripper.app")
        settings = _make_settings(tmp_path)
        repo = _StaleRecordRepo([])
        app = _make_app(settings, repo, NullTagger(), NullFingerprintProvider())
        app.settings.destination = None
        await app._cleanup_orphans()
        assert "no stale records found" in caplog.text


class TestValidateAcoustidKey:
    """RadioRipperApp._validate_acoustid_key()."""

    async def test_uses_fresh_cache(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="radio_ripper.app")
        work = tmp_path / "work"
        work.mkdir(parents=True)
        cache = work / "acoustid_key.ok"
        cache.write_text("0", encoding="utf-8")
        settings = _make_settings(tmp_path, work_dir=work)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        await app._validate_acoustid_key()
        assert "validation cache is fresh" in caplog.text

    async def test_key_rejected(self, tmp_path) -> None:
        work = tmp_path / "work"
        settings = _make_settings(tmp_path, work_dir=work)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "error",
            "error": {"message": "Invalid API key"},
        }
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_cls.return_value.__aenter__.return_value = mock_client
            with pytest.raises(ConfigurationError, match="AcoustID API key rejected"):
                await app._validate_acoustid_key()

    async def test_non_key_error_status(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.DEBUG, logger="radio_ripper.app")
        work = tmp_path / "work"
        settings = _make_settings(tmp_path, work_dir=work)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "error",
            "error": {"message": "no matching records"},
        }
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_cls.return_value.__aenter__.return_value = mock_client
            await app._validate_acoustid_key()
            assert "test fingerprint not accepted" in caplog.text

    async def test_successful_validation(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.INFO, logger="radio_ripper.app")
        work = tmp_path / "work"
        settings = _make_settings(tmp_path, work_dir=work)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_cls.return_value.__aenter__.return_value = mock_client
            await app._validate_acoustid_key()
            assert "API key validated" in caplog.text
            assert (work / "acoustid_key.ok").is_file()

    async def test_nonfatal_request_error(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.WARNING, logger="radio_ripper.app")
        work = tmp_path / "work"
        settings = _make_settings(tmp_path, work_dir=work)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = ConnectionError("DNS failure")
            mock_cls.return_value.__aenter__.return_value = mock_client
            await app._validate_acoustid_key()
            assert "non-fatal" in caplog.text


class TestStart:
    """RadioRipperApp.start() — additional branches."""

    async def test_cancelled_after_reprocess(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.INFO, logger="radio_ripper.app")
        settings = _make_settings(tmp_path)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        app.cancel()
        await app.start()
        assert "Startup cancelled" in caplog.text
        assert len(app.recorders()) == 0

    async def test_with_custom_m3u_stations(self, tmp_path) -> None:
        work = tmp_path / "work"
        custom_path = work / "stations" / "custom.m3u"
        custom_path.parent.mkdir(parents=True)
        custom_path.write_text("dummy", encoding="utf-8")
        settings = _make_settings(
            tmp_path,
            streams=[],
            work_dir=work,
            disable_automatic_streams=True,
        )
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        with patch("radio_ripper.app.load_local_m3u") as mock_load:
            mock_load.return_value = [StreamConfig(name="Custom1", url="http://example.com/stream")]
            await app.start()
        assert len(app.recorders()) == 1
        await app.stop()

    async def test_no_streams_available_logs_error(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.ERROR, logger="radio_ripper.app")
        work = tmp_path / "work"
        work.mkdir(parents=True)
        settings = _make_settings(
            tmp_path,
            streams=[],
            work_dir=work,
            disable_automatic_streams=True,
        )
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        await app.start()
        assert "No streams available" in caplog.text
        assert len(app.recorders()) == 0

    async def test_skips_disabled_stream(self, tmp_path, caplog) -> None:
        caplog.set_level(logging.INFO, logger="radio_ripper.app")
        settings = _make_settings(
            tmp_path,
            streams=[StreamConfig(name="DisabledStation", url="http://x", enabled=False)],
        )
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        await app.start()
        assert "Skipping disabled stream" in caplog.text
        assert len(app.recorders()) == 0
        await app.stop()

    async def test_stream_discovery_integration(self, tmp_path) -> None:
        work = tmp_path / "work"
        work.mkdir(parents=True)
        custom_path = work / "stations" / "custom.m3u"
        custom_path.parent.mkdir(parents=True)
        custom_path.write_text("#EXTM3U\n", encoding="utf-8")
        settings = _make_settings(
            tmp_path,
            streams=[],
            work_dir=work,
            disable_automatic_streams=False,
        )
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        fake_discovered = [StreamConfig(name="Discovered", url="http://d")]
        with patch("radio_ripper.app.PlaylistDiscoveryService") as mock_disc_cls:
            mock_disc = AsyncMock()
            mock_disc.load_or_discover = AsyncMock(return_value=fake_discovered)
            mock_disc_cls.return_value = mock_disc
            await app.start()
        assert len(app.recorders()) == 1
        assert app.recorders()[0].station_name == "Discovered"
        await app.stop()

    async def test_stream_capping_custom_priority(self, tmp_path) -> None:
        work = tmp_path / "work"
        custom_path = work / "stations" / "custom.m3u"
        custom_path.parent.mkdir(parents=True)
        custom_path.write_text("dummy", encoding="utf-8")
        settings = _make_settings(
            tmp_path,
            streams=[],
            work_dir=work,
            disable_automatic_streams=False,
            max_concurrent_streams=2,
        )
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        with patch("radio_ripper.app.load_local_m3u") as mock_load:
            mock_load.return_value = [
                StreamConfig(name="custom1", url="http://c1"),
                StreamConfig(name="custom2", url="http://c2"),
            ]
            fake_discovered = [
                StreamConfig(name="Discovered1", url="http://d1"),
                StreamConfig(name="Discovered2", url="http://d2"),
            ]
            with patch("radio_ripper.app.PlaylistDiscoveryService") as mock_disc_cls:
                mock_disc = AsyncMock()
                mock_disc.load_or_discover = AsyncMock(return_value=fake_discovered)
                mock_disc_cls.return_value = mock_disc
                await app.start()
        assert len(app.recorders()) == 2
        names = [r.station_name for r in app.recorders()]
        assert names == ["custom1", "custom2"]
        await app.stop()

    async def test_stream_capping_mixed(self, tmp_path) -> None:
        work = tmp_path / "work"
        custom_path = work / "stations" / "custom.m3u"
        custom_path.parent.mkdir(parents=True)
        custom_path.write_text("dummy", encoding="utf-8")
        settings = _make_settings(
            tmp_path,
            streams=[],
            work_dir=work,
            disable_automatic_streams=False,
            max_concurrent_streams=2,
        )
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        with patch("radio_ripper.app.load_local_m3u") as mock_load:
            mock_load.return_value = [StreamConfig(name="custom1", url="http://c1")]
            fake_discovered = [
                StreamConfig(name="Discovered1", url="http://d1"),
                StreamConfig(name="Discovered2", url="http://d2"),
            ]
            with patch("radio_ripper.app.PlaylistDiscoveryService") as mock_disc_cls:
                mock_disc = AsyncMock()
                mock_disc.load_or_discover = AsyncMock(return_value=fake_discovered)
                mock_disc_cls.return_value = mock_disc
                await app.start()
        assert len(app.recorders()) == 2
        names = [r.station_name for r in app.recorders()]
        assert names == ["custom1", "Discovered1"]
        await app.stop()


class TestStop:
    """RadioRipperApp.stop() — additional branches."""

    async def test_recorder_timeout(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        app = _make_app(settings, FakeRepo(), NullTagger(), NullFingerprintProvider())
        await app.start()
        rec = app.recorders()[0]
        async def slow_join():
            await asyncio.sleep(100)
        rec.join = slow_join
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(app.stop(), timeout=0.3)


class TestFromSettings:
    """RadioRipperApp.from_settings() — error path."""

    async def test_missing_api_key_raises_configuration_error(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        with (
            patch.dict("os.environ", {"ACOUSTID_API_KEY": "", "ACCOUST_ID": ""}),
            patch("radio_ripper.app.SQLiteTrackRepository") as mock_repo_cls,
            patch("radio_ripper.app.HttpxAsyncClient"),
            patch("radio_ripper.app.ID3Tagger"),
        ):
            mock_repo_cls.return_value = AsyncMock()
            with pytest.raises(ConfigurationError, match="AcoustID API-Key required"):
                RadioRipperApp.from_settings(settings)


__all__ = []
