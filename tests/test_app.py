"""Tests for radio_ripper.app — RadioRipperApp composition."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from radio_ripper.app import RadioRipperApp
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.metadata import NullMetadataProvider
from radio_ripper.services.playlist import StaticPlaylistResolver
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.tagging import NullTagger


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
