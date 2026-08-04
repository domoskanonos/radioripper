"""Tests for radio_ripper.app — RadioRipperApp composition (stream mode)."""

from __future__ import annotations

import errno
import json
import os
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from radio_ripper.app import RadioRipperApp
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.playlist import StaticPlaylistResolver
from radio_ripper.services.playlist_discovery import PlaylistDiscoveryService


@pytest.fixture(autouse=True)
def _mock_probe_icy():
    """Prevent pre-flight check from making real HTTP connections."""
    with patch("radio_ripper.app.probe_icy", return_value={"icy": True, "bitrate": 128, "error": None}):
        yield


def _make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "work_dir": str(tmp_path / "work"),
        "destination": str(tmp_path / "work" / "destination"),
        "discovery_enabled": False,
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
        with patch.object(
            PlaylistDiscoveryService,
            "load_or_discover",
            return_value=[StreamConfig(name="TestStation", url="http://fake.example.com/listen.m3u")],
        ):
            await app.start()
        assert len(app.recorders()) == 1
        await app.stop()

    async def test_no_streams_logs_error(self, tmp_path, caplog) -> None:
        settings = Settings.model_validate(
            {
                "work_dir": str(tmp_path / "work"),
                "destination": str(tmp_path / "work" / "destination"),
            }
        )
        client = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        with patch.object(PlaylistDiscoveryService, "load_or_discover", return_value=[]):
            await app.start()
        assert len(app.recorders()) == 0
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


class TestRadioRipperAppStreamLimit:
    def test_no_limit_when_below_max(self, tmp_path):
        stations = [StreamConfig(name=f"S{i}", url=f"http://x/{i}") for i in range(3)]
        settings = _make_settings(tmp_path, max_concurrent_streams=10)
        app = _make_app(settings)
        assert app._apply_stream_limit(stations) == stations

    def test_truncates_when_over_max_and_no_custom(self, tmp_path):
        stations = [StreamConfig(name=f"S{i}", url=f"http://x/{i}") for i in range(5)]
        settings = _make_settings(tmp_path, max_concurrent_streams=3)
        app = _make_app(settings)
        result = app._apply_stream_limit(stations)
        assert len(result) == 3
        assert result == stations[:3]


class TestRadioRipperAppStreamClient:
    def test_pool_follows_max_concurrent_streams(self, tmp_path):
        settings = _make_settings(tmp_path, max_concurrent_streams=2000)
        app = RadioRipperApp.from_settings(settings)
        pool = app.client._client._transport._pool
        assert pool._max_connections == 2000
        assert app.client._client._timeout.pool == 30.0

    def test_pool_size_override(self, tmp_path):
        settings = _make_settings(tmp_path, max_concurrent_streams=2000, http_pool_size=500)
        app = RadioRipperApp.from_settings(settings)
        assert app.client._client._transport._pool._max_connections == 500


class TestRadioRipperAppPreflight:
    async def test_all_stations_reachable(self, tmp_path):
        with patch("radio_ripper.app.probe_icy", return_value={"icy": True, "error": None}):
            settings = _make_settings(tmp_path)
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
        settings = _make_settings(tmp_path, discovery_enabled=False)
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
        settings = _make_settings(tmp_path)
        client = AsyncMock()
        client.aclose = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        with patch.object(
            PlaylistDiscoveryService,
            "load_or_discover",
            return_value=[StreamConfig(name="Enabled", url="http://y")],
        ):
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


class TestBackpressure:
    def test_no_backpressure_by_default(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        assert app._backpressure_reason() is None

    def test_destination_over_inbox_limit(self, tmp_path):
        settings = _make_settings(tmp_path, max_files_inbox=2)
        app = _make_app(settings)
        dest = settings.destination
        dest.mkdir(parents=True)
        for i in range(3):
            (dest / f"song{i}.mp3").write_bytes(b"x")
        reason = app._backpressure_reason()
        assert reason is not None
        assert "max_files_inbox" in reason

    def test_staging_over_file_limit(self, tmp_path):
        settings = _make_settings(tmp_path, max_unchecked_files=100)
        app = _make_app(settings)
        staging = settings.work_dir / "unchecked_mp3"
        staging.mkdir(parents=True)
        for i in range(101):
            (staging / f"pending{i}.mp3").write_bytes(b"x")
        reason = app._backpressure_reason()
        assert reason is not None
        assert "max_unchecked_files" in reason

    def test_staging_over_byte_limit(self, tmp_path):
        settings = _make_settings(tmp_path, max_unchecked_bytes=100)
        app = _make_app(settings)
        staging = settings.work_dir / "unchecked_mp3"
        staging.mkdir(parents=True)
        (staging / "big.mp3").write_bytes(b"x" * 200)
        reason = app._backpressure_reason()
        assert reason is not None
        assert "max_unchecked_bytes" in reason

    @pytest.mark.asyncio
    async def test_check_backpressure_pauses_on_limit(self, tmp_path):
        settings = _make_settings(tmp_path, max_files_inbox=1)
        app = _make_app(settings)
        dest = settings.destination
        dest.mkdir(parents=True)
        (dest / "a.mp3").write_bytes(b"x")
        with patch.object(app, "_pause_all") as pause, patch.object(app, "_resume_all") as resume:
            await app._check_backpressure()
        pause.assert_called_once()
        resume.assert_not_called()
        assert app._backpressure_paused is True

    @pytest.mark.asyncio
    async def test_check_backpressure_resumes_when_cleared(self, tmp_path):
        settings = _make_settings(tmp_path, max_files_inbox=2)
        app = _make_app(settings)
        app._backpressure_paused = True
        with patch.object(app, "_resume_all") as resume, patch.object(app, "_pause_all") as pause:
            await app._check_backpressure()
        resume.assert_called_once()
        pause.assert_not_called()
        assert app._backpressure_paused is False

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


class TestConfigReload:
    """Config-change reload: one shared start path re-run."""

    def _live_app(self, tmp_path, **overrides):
        base = {
            "work_dir": str(tmp_path / "work"),
            "destination": str(tmp_path / "work" / "destination"),
            "log_level": "INFO",
        }
        base.update(overrides)
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(base))
        settings = Settings.model_validate(base)
        app = RadioRipperApp.from_settings_with_live_config(settings, config_path)
        return app, config_path, base

    def _write_change(self, config_path, base, **changes):
        cfg = dict(base)
        cfg.update(changes)
        config_path.write_text(json.dumps(cfg))
        # guarantee a distinct mtime even on coarse filesystems
        t = time.time() + 2
        os.utime(config_path, (t, t))

    def test_reload_fields_exclude_cheap_handlers(self):
        from radio_ripper.app import _RELOAD_FIELDS

        assert "log_level" not in _RELOAD_FIELDS
        assert "max_files_inbox" not in _RELOAD_FIELDS
        assert "max_concurrent_streams" in _RELOAD_FIELDS
        assert "stream_keywords" in _RELOAD_FIELDS
        assert "discovery_min_bitrate" in _RELOAD_FIELDS
        assert "work_dir" in _RELOAD_FIELDS

    async def test_process_config_reload_triggers_full_reload(self, tmp_path):
        app, config_path, base = self._live_app(tmp_path)
        app._reload_after_config_change = AsyncMock()

        self._write_change(config_path, base, max_concurrent_streams=123)
        await app._process_config_reload()

        app._reload_after_config_change.assert_awaited_once()
        assert app.settings.max_concurrent_streams == 123

    async def test_process_config_reload_skips_reload_for_log_level(self, tmp_path):
        app, config_path, base = self._live_app(tmp_path)
        app._reload_after_config_change = AsyncMock()

        self._write_change(config_path, base, log_level="DEBUG")
        await app._process_config_reload()

        app._reload_after_config_change.assert_not_awaited()
        assert app.settings.log_level == "DEBUG"

    async def test_reload_after_config_change_restarts_recorders(self, tmp_path):
        settings = _make_settings(tmp_path)
        app = _make_app(settings)
        with patch.object(
            PlaylistDiscoveryService,
            "load_or_discover",
            return_value=[StreamConfig(name="TestStation", url="http://fake.example.com/listen.m3u")],
        ):
            await app.start()
            assert len(app.recorders()) == 1
            old = app.recorders()[0]

            await app._reload_after_config_change()

            assert len(app.recorders()) == 1
            new = app.recorders()[0]
            assert new is not old
            assert old._stop_event.is_set()
        await app.stop()

    async def test_reload_reruns_discovery_and_clears_stations(self, tmp_path):
        settings = Settings.model_validate(
            {
                "work_dir": str(tmp_path / "work"),
                "destination": str(tmp_path / "destination"),
                "discovery_enabled": True,
            }
        )
        client = AsyncMock()
        client.aclose = AsyncMock()
        app = RadioRipperApp(
            settings=settings,
            client=client,
            playlist_resolver=StaticPlaylistResolver(["http://x"]),
        )
        with patch(
            "radio_ripper.app.PlaylistDiscoveryService.load_or_discover",
            return_value=[StreamConfig(name="D1", url="http://d1")],
        ):
            await app.start()
            assert len(app.recorders()) == 1
        with patch(
            "radio_ripper.app.PlaylistDiscoveryService.load_or_discover",
            return_value=[],
        ) as discover2:
            await app._reload_after_config_change()
        discover2.assert_awaited_once()
        assert app.recorders() == []
        await app.stop()


class TestMigrateUnscoredFiles:
    async def test_migrates_unscored_files_across_devices(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        dest = settings.destination
        dest.mkdir(parents=True)
        payload = b"\xff\xe0\x90\x00" + b"\x00" * 100
        legacy = dest / "legacy.mp3"
        legacy.write_bytes(payload)

        app = _make_app(settings)
        app._acoustid_queue = Mock()
        app._acoustid_queue.unchecked_dir = settings.work_dir / "unchecked_mp3"
        app._acoustid_queue.unchecked_dir.mkdir(parents=True)

        with patch("radio_ripper.services.storage.os.replace", side_effect=OSError(errno.EXDEV, "cross-device link")):
            await app._migrate_unscored_files()

        assert not legacy.exists()
        moved = list(app._acoustid_queue.unchecked_dir.glob("*.mp3"))
        assert len(moved) == 1
        assert moved[0].read_bytes() == payload
        app._acoustid_queue.enqueue.assert_called_once_with(moved[0])

    async def test_scored_files_are_not_migrated(self, tmp_path) -> None:
        settings = _make_settings(tmp_path)
        dest = settings.destination
        dest.mkdir(parents=True)
        from radio_ripper.services.storage import write_mp3_tags

        scored = dest / "scored.mp3"
        scored.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 100)
        write_mp3_tags(scored, artist="Artist", title="Title", score=0.95)

        app = _make_app(settings)
        app._acoustid_queue = Mock()
        app._acoustid_queue.unchecked_dir = settings.work_dir / "unchecked_mp3"

        await app._migrate_unscored_files()

        assert scored.exists()
        app._acoustid_queue.enqueue.assert_not_called()


__all__ = []
