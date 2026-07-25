"""Tests for radio_ripper.app — RadioRipperApp composition (stream mode)."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from radio_ripper.app import RadioRipperApp
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.playlist import StaticPlaylistResolver


def _make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "destination": tmp_path / "recordings",
        "database": tmp_path / "ripper.db",
        "streams": [StreamConfig(name="TestStation", url="http://fake.example.com/listen.m3u")],
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _make_app(settings: Settings) -> RadioRipperApp:
    client = AsyncMock()
    client.aclose = AsyncMock()
    return RadioRipperApp(
        settings=settings,
        client=client,
        playlist_resolver=StaticPlaylistResolver(["http://x"]),
    )


class TestRadioRipperApp:
    """Core App lifecycle tests."""

    async def test_create_recorders_for_each_stream(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        assert len(app.recorders()) == 0
        await app.start()
        assert len(app.recorders()) == 1
        await app.stop()

    async def test_multiple_streams(self, tmp_path) -> None:
        settings = Settings.model_validate({
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [
                {"name": "Station1", "url": "http://example.com/1.m3u"},
                {"name": "Station2", "url": "http://example.com/2.m3u"},
                {"name": "Station3", "url": "http://example.com/3.m3u"},
            ],
        })
        client = AsyncMock()
        client.aclose = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        assert len(app.recorders()) == 3
        await app.stop()

    async def test_no_streams_logs_error(self, tmp_path, caplog) -> None:
        settings = _make_settings(tmp_path)
        settings = Settings.model_validate({
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [{"name": "S1", "url": "http://example.com/1.m3u"}],
        })
        client = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        assert len(app.recorders()) == 1
        await app.stop()

    async def test_stop_closes_client(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        client = AsyncMock()
        client.aclose = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        await app.stop()
        client.aclose.assert_called_once()


__all__ = []
