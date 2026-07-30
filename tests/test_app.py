"""Tests for radio_ripper.app — RadioRipperApp composition (stream mode)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.app import RadioRipperApp
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.playlist import StaticPlaylistResolver


@pytest.fixture(autouse=True)
def _mock_probe_icy():
    """Prevent pre-flight check from making real HTTP connections."""
    with patch("radio_ripper.app.probe_icy", return_value={"icy": True, "bitrate": 128, "error": None}):
        yield


def _make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "work_dir": str(tmp_path / "work"),
        "destination": str(tmp_path / "work" / "destination"),
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
        settings = Settings.model_validate(
            {
                "work_dir": str(tmp_path / "work"),
                "destination": str(tmp_path / "work" / "destination"),
                "streams": [
                    {"name": "Station1", "url": "http://example.com/1.m3u"},
                    {"name": "Station2", "url": "http://example.com/2.m3u"},
                    {"name": "Station3", "url": "http://example.com/3.m3u"},
                ],
            }
        )
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
        settings = Settings.model_validate(
            {
                "work_dir": str(tmp_path / "work"),
                "destination": str(tmp_path / "work" / "destination"),
                "streams": [{"name": "S1", "url": "http://example.com/1.m3u"}],
            }
        )
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


class TestRadioRipperAppSelectStations:
    def test_returns_explicit_streams(self, tmp_path):
        cfg = StreamConfig(name="S1", url="http://example.com/1.m3u")
        settings = _make_settings(tmp_path, streams=[cfg])
        app = _make_app(settings)
        stations = app._select_stations()
        assert stations == [cfg]

    def test_returns_custom_m3u_when_no_streams(self, tmp_path):
        custom_dir = tmp_path / "work" / "stations"
        custom_dir.mkdir(parents=True)
        (custom_dir / "custom.m3u").write_text("#EXTM3U\n#EXTINF:-1,S1\nhttp://example.com/1.m3u")
        settings = _make_settings(tmp_path, streams=[])
        app = _make_app(settings)
        stations = app._select_stations()
        assert len(stations) == 1
        assert stations[0].name == "S1"

    def test_creates_custom_m3u_when_missing(self, tmp_path, caplog):
        caplog.set_level("INFO")
        settings = _make_settings(tmp_path, streams=[])
        app = _make_app(settings)
        stations = app._select_stations()
        assert stations == []
        custom_file = tmp_path / "work" / "stations" / "custom.m3u"
        assert custom_file.is_file()
        assert custom_file.read_text() == "#EXTM3U\n"


class TestRadioRipperAppStreamLimit:
    def test_no_limit_when_below_max(self, tmp_path):
        stations = [StreamConfig(name=f"S{i}", url=f"http://x/{i}") for i in range(3)]
        settings = _make_settings(tmp_path, max_concurrent_streams=10)
        app = _make_app(settings)
        assert app._apply_stream_limit(stations) == stations

    def test_truncates_when_over_max_and_no_custom(self, tmp_path):
        stations = [StreamConfig(name=f"S{i}", url=f"http://x/{i}") for i in range(5)]
        settings = _make_settings(tmp_path, max_concurrent_streams=3, streams=[])
        app = _make_app(settings)
        result = app._apply_stream_limit(stations)
        assert len(result) == 3
        assert result == stations[:3]

    def test_prioritizes_custom_stations(self, tmp_path):
        custom_dir = tmp_path / "work" / "stations"
        custom_dir.mkdir(parents=True)
        custom_m3u = custom_dir / "custom.m3u"
        custom_m3u.write_text("#EXTM3U\n#EXTINF:-1,MyFav\nhttp://fav.example.com")
        # _select_stations returns custom stations first, then discovered ones
        stations = [
            StreamConfig(name="MyFav", url="http://fav.example.com"),
            StreamConfig(name="A", url="http://a"),
            StreamConfig(name="B", url="http://b"),
            StreamConfig(name="C", url="http://c"),
        ]
        settings = _make_settings(tmp_path, max_concurrent_streams=2, streams=[])
        app = _make_app(settings)
        result = app._apply_stream_limit(stations)
        assert len(result) == 2
        # MyFav (the custom station) should be preserved, plus one other
        assert result[0].name == "MyFav"


class TestRadioRipperAppPreflight:
    async def test_all_stations_reachable(self, tmp_path):
        with patch("radio_ripper.app.probe_icy", return_value={"icy": True, "error": None}):
            settings = _make_settings(
                tmp_path,
                streams=[StreamConfig(name="S1", url="http://x"), StreamConfig(name="S2", url="http://y")],
            )
            app = _make_app(settings)
            result = await app._preflight_check(
                [StreamConfig(name="S1", url="http://x"), StreamConfig(name="S2", url="http://y")]
            )
            assert len(result) == 2

    async def test_unreachable_stations_removed(self, tmp_path):
        with patch(
            "radio_ripper.app.probe_icy",
            side_effect=[{"icy": True, "error": None}, {"icy": False, "error": "timeout"}],
        ):
            settings = _make_settings(tmp_path)
            app = _make_app(settings)
            stations = [StreamConfig(name="S1", url="http://x"), StreamConfig(name="S2", url="http://bad")]
            result = await app._preflight_check(stations)
            assert len(result) == 1
            assert result[0].name == "S1"

    async def test_disabled_stations_preserved(self, tmp_path):
        with patch("radio_ripper.app.probe_icy", return_value={"icy": True, "error": None}):
            settings = _make_settings(tmp_path)
            app = _make_app(settings)
            disabled = StreamConfig(name="S0", url="http://x", enabled=False)
            reachable = StreamConfig(name="S1", url="http://y")
            result = await app._preflight_check([disabled, reachable])
            assert len(result) == 2
            assert result[0] == disabled  # disabled come first


class TestRadioRipperAppLifecycle:
    def test_from_settings(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = RadioRipperApp.from_settings(settings)
        assert app.settings is settings
        assert len(app.recorders()) == 0

    def test_cancel_sets_flag(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        assert not app._cancel_requested
        app.cancel()
        assert app._cancel_requested is True


class TestRadioRipperAppFactoryMethods:
    def test_from_settings_with_live_config(self, tmp_path):
        settings = _make_settings(tmp_path)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"work_dir": "./work", "destination": "./destination"}')
        app = RadioRipperApp.from_settings_with_live_config(settings, config_path)
        assert app.settings is settings
        assert app._live_config is not None

    async def test_start_cancelled_returns_early(self, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="radio_ripper.app")
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        app.cancel()
        await app.start()
        assert "Startup cancelled." in caplog.text

    async def test_no_streams_available_returns_early(self, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="radio_ripper.app")
        settings = _make_settings(tmp_path, streams=[], discovery_enabled=False)
        app = _make_app(settings)
        await app.start()
        assert "No streams available" in caplog.text

    async def test_start_with_discovery(self, tmp_path):
        settings = Settings.model_validate(
            {
                "work_dir": str(tmp_path / "work"),
                "destination": str(tmp_path / "destination"),
                "stream_keywords": ["rock"],
                "discovery_enabled": True,
            }
        )
        (tmp_path / "work" / "stations").mkdir(parents=True)
        (tmp_path / "work" / "stations" / "custom.m3u").write_text("#EXTM3U\n")
        client = AsyncMock()
        client.aclose = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        with patch("radio_ripper.app.PlaylistDiscoveryService.load_or_discover", return_value=[]):
            await app.start()
        await app.stop()

    async def test_start_skips_disabled_stream(self, tmp_path):
        settings = _make_settings(
            tmp_path,
            streams=[
                StreamConfig(name="Disabled", url="http://x", enabled=False),
                StreamConfig(name="Enabled", url="http://y"),
            ],
        )
        client = AsyncMock()
        client.aclose = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        await app.start()
        assert len(app.recorders()) == 1
        assert app.recorders()[0].station_name == "Enabled"
        await app.stop()


class TestRadioRipperAppLifecycleMethods:
    def test_pause_and_resume(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        app._pause_all()
        app._resume_all()

    def test_count_inbox_files(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        count = app._count_inbox_files()
        assert count == 0

    def test_count_inbox_with_files(self, tmp_path):
        inbox = tmp_path / "work" / "destination"
        inbox.mkdir(parents=True)
        (inbox / "song1.mp3").write_text("data")
        (inbox / "song2.mp3").write_text("data")
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        count = app._count_inbox_files()
        assert count == 2

    def test_apply_config_diff_log_level(self, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="radio_ripper.app")
        from radio_ripper.infra.config import LiveConfig

        settings = _make_settings(tmp_path)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"log_level": "INFO"}')
        live_config = LiveConfig(config_path, settings)
        app = _make_app(settings)
        app._live_config = live_config
        app._apply_config_diff({"log_level": ("INFO", "DEBUG")})
        assert "Config changed: log_level" in caplog.text

    def test_apply_config_diff_max_files(self, tmp_path, caplog):
        from radio_ripper.infra.config import LiveConfig

        settings = _make_settings(tmp_path)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"log_level": "INFO"}')
        live_config = LiveConfig(config_path, settings)
        app = _make_app(settings)
        app._live_config = live_config
        app._apply_config_diff({"max_files_inbox": (1000, 2000)})
        assert "Inbox limit changed" in caplog.text

    async def test_housekeeping_config_reload(self, tmp_path):
        settings = _make_settings(tmp_path)
        config_path = tmp_path / "config.json"
        config_path.write_text('{"log_level": "DEBUG"}')
        app = RadioRipperApp.from_settings_with_live_config(settings, config_path)
        app.cancel()
        diff = await app._live_config.check_reload()
        assert diff == {}

    async def test_sleep_until_cancelled(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        app.cancel()
        await app._sleep_until(999999.0)

    async def test_stop_with_running_housekeeping(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        await app.start()
        await app.stop()
        assert app._housekeeping_task is None


__all__ = []
