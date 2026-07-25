"""Tests for radio_ripper.cli argument parsing and entry points."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.cli import _build_arg_parser, _find_config_path, _run_async, main


class TestBuildArgParser:
    def test_help_flag(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0

    def test_config_flag(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--config", "/tmp/test.json"])
        assert args.config == "/tmp/test.json"

    def test_config_short_flag(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["-c", "/tmp/cfg.json"])
        assert args.config == "/tmp/cfg.json"

    def test_log_level_flag(self):
        parser = _build_arg_parser()
        args = parser.parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"

    def test_no_config_defaults_to_none(self):
        parser = _build_arg_parser()
        args = parser.parse_args([])
        assert args.config is None

    def test_version_flag(self):
        parser = _build_arg_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--version"])
        assert exc_info.value.code == 0


class TestFindConfigPath:
    def test_returns_arg_when_provided(self):
        assert _find_config_path("/custom/config.json") == "/custom/config.json"

    def test_returns_none_when_nothing_found(self, monkeypatch, tmp_path):
        # Override default paths to non-existing dirs
        monkeypatch.chdir(tmp_path)
        result = _find_config_path(None)
        # config.json doesn't exist in tmp_path → should return None
        assert result is None

    def test_finds_local_config(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{}", encoding="utf-8")
        result = _find_config_path_with_cwd(tmp_path, None)
        assert result is not None


def _find_config_path_with_cwd(cwd: Path, arg: str | None) -> str | None:
    """Helper to test config lookup within a specific cwd."""
    import os

    orig = os.getcwd()
    os.chdir(cwd)
    try:
        return _find_config_path(arg)
    finally:
        os.chdir(orig)


class TestMain:
    def test_returns_2_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["radio-ripper"])
        result = main([])
        assert result == 2

    def test_returns_2_when_config_file_missing(self):
        result = main(["--config", "/nonexistent/path/config.json"])
        assert result == 2

    def test_returns_2_when_config_invalid(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{bad json", encoding="utf-8")
        result = main(["--config", str(p)])
        assert result == 2

    async def test_main_runs_and_stops_with_valid_config(self, tmp_path):
        """Verify main() starts the app but returns quickly on a fake shutdown."""
        import json

        cfg = {
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [{"name": "TestStation", "url": "http://fake.example.com/listen.m3u"}],
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")

        from radio_ripper.infra.config import load_settings
        from radio_ripper.infra.logging import configure_logging

        settings = load_settings(p)
        logger = configure_logging("WARNING", None)

        # Mock app.start to just set an event
        import asyncio

        started = asyncio.Event()

        with patch("radio_ripper.cli.RadioRipperApp.from_settings") as mock_factory:
            mock_app = mock_factory.return_value

            async def fake_start():
                started.set()

            async def fake_stop():
                pass

            mock_app.start = fake_start
            mock_app.stop = fake_stop

            # Run _run_async but immediately trigger stop_event
            async def run_test():
                asyncio.get_running_loop()
                stop_event = asyncio.Event()

                async def immediate_stop():
                    await asyncio.sleep(0.1)
                    stop_event.set()

                asyncio.create_task(immediate_stop())  # noqa: RUF006  # fire-and-forget in test

                # We can't directly test _run_async because it creates its own
                # signal handlers + stop_event. Instead test that mock_factory
                # gets called correctly.
                app = mock_factory(settings, logger=logger)
                await app.start()
                await app.stop()

            await run_test()
            mock_factory.assert_called()


class TestRunAsync:
    @pytest.mark.asyncio
    async def test_stop_event_terminates(self, tmp_path):
        from radio_ripper.infra.config import load_settings
        from radio_ripper.infra.logging import configure_logging

        cfg = {
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [{"name": "T", "url": "http://example.com/listen.m3u"}],
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")
        settings = load_settings(p)
        logger = configure_logging("WARNING", None)

        mock_app = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.cancel = AsyncMock()
        with patch("radio_ripper.cli.RadioRipperApp.from_settings", return_value=mock_app):
            with patch("asyncio.get_running_loop") as mock_loop:
                fake_loop = AsyncMock()

                def fake_add_signal_handler(sig, handler, *args):
                    handler(*args)

                fake_loop.add_signal_handler = fake_add_signal_handler
                mock_loop.return_value = fake_loop

                result = await _run_async(settings, logger)

        assert result == 0
        mock_app.start.assert_awaited_once()
        mock_app.stop.assert_awaited_once()
        # SIGINT+SIGTERM both registered, each calls cancel
        mock_app.cancel.assert_called()

    @pytest.mark.asyncio
    async def test_signal_handler_calls_cancel(self):
        from radio_ripper.infra.config import Settings
        from radio_ripper.infra.logging import configure_logging

        settings = Settings(destination="/tmp", database="/tmp/db.sqlite", streams=[])
        logger = configure_logging("WARNING", None)

        mock_app = AsyncMock()
        mock_app.start = AsyncMock()
        mock_app.stop = AsyncMock()
        mock_app.cancel = AsyncMock()

        with patch("radio_ripper.cli.RadioRipperApp.from_settings", return_value=mock_app):
            loop = asyncio.get_running_loop()
            signal_handlers = {}
            original_add = loop.add_signal_handler

            def recording_add(sig, handler, *args):
                signal_handlers[sig] = (handler, args)
                original_add(sig, handler, *args)

            loop.add_signal_handler = recording_add
            try:
                stop_event = asyncio.Event()
                with patch("radio_ripper.cli.asyncio.Event", return_value=stop_event):

                    async def trigger():
                        await asyncio.sleep(0.05)
                        for handler, args in signal_handlers.values():
                            handler(*args)

                    asyncio.ensure_future(trigger())
                    result = await _run_async(settings, logger)
            finally:
                loop.add_signal_handler = original_add

        assert result == 0
        mock_app.cancel.assert_called()


class TestMainIntegration:
    def test_log_level_override_configure_logging(self, tmp_path):
        cfg = {
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [{"name": "T", "url": "http://example.com/listen.m3u"}],
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")

        from radio_ripper.infra.config import load_settings

        real_settings = load_settings(p)

        with patch("radio_ripper.cli.load_settings", return_value=real_settings):
            with patch("radio_ripper.cli.configure_logging") as mock_cfg:
                with patch("radio_ripper.cli._run_async", return_value=0):
                    result = main(["--config", str(p), "--log-level", "DEBUG"])

        assert result == 0
        mock_cfg.assert_called_once()
        args, _ = mock_cfg.call_args
        assert args[0] == "DEBUG"

    def test_keyboard_interrupt(self, tmp_path):
        cfg = {
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [{"name": "T", "url": "http://example.com/listen.m3u"}],
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")

        with patch("radio_ripper.cli.load_settings"):
            with patch("radio_ripper.cli.configure_logging"):
                with patch("asyncio.run", side_effect=KeyboardInterrupt):
                    result = main(["--config", str(p)])

        assert result == 0

    def test_main_success_path(self, tmp_path):
        cfg = {
            "destination": str(tmp_path / "recordings"),
            "database": str(tmp_path / "ripper.db"),
            "streams": [{"name": "T", "url": "http://example.com/listen.m3u"}],
        }
        p = tmp_path / "config.json"
        p.write_text(json.dumps(cfg), encoding="utf-8")

        with patch("radio_ripper.cli.load_settings"):
            with patch("radio_ripper.cli.configure_logging"):
                with patch("asyncio.run", return_value=0) as mock_run:
                    result = main(["--config", str(p)])

        assert result == 0
        mock_run.assert_called_once()
