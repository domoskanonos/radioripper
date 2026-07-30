"""Tests for radio_ripper.cli (stream)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.cli import main


class TestCli:
    def test_help(self):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_minimal_config_path(self, tmp_path: Path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(
            json.dumps(
                {
                    "work_dir": str(tmp_path),
                    "destination": str(tmp_path / "destination"),
                }
            )
        )
        with patch("radio_ripper.cli._run") as mock:
            main(["--config", str(cfg)])
        mock.assert_called_once()

    def test_missing_config_returns_2(self):
        rc = main(["--config", "/nonexistent/config.json"])
        assert rc == 2

    def test_version(self):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0

    def test_log_level_override(self, tmp_path: Path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"work_dir": str(tmp_path), "destination": str(tmp_path / "inbox")}))
        with patch("radio_ripper.cli._run") as mock:
            main(["--config", str(cfg), "--log-level", "DEBUG"])
        mock.assert_called_once()

    def test_keyboard_interrupt_returns_0(self, tmp_path: Path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"work_dir": str(tmp_path), "destination": str(tmp_path / "inbox")}))
        with patch("radio_ripper.cli._run", side_effect=KeyboardInterrupt):
            rc = main(["--config", str(cfg)])
        assert rc == 0


class TestRun:
    @patch("shutil.which", return_value="/usr/bin/ffprobe")
    async def test_run_no_config(self, _mock_which, tmp_path):
        from radio_ripper.cli import _run
        from radio_ripper.infra.config import Settings

        logger = logging.getLogger("radio_ripper.test")
        settings = Settings.model_validate({"work_dir": str(tmp_path), "destination": str(tmp_path / "inbox")})
        loop = asyncio.get_running_loop()
        with (
            patch("radio_ripper.app.RadioRipperApp") as mock_app_cls,
            patch.object(loop, "add_signal_handler"),
            patch("asyncio.Event.wait", new=AsyncMock(return_value=None)),
        ):
            mock_app = mock_app_cls.from_settings.return_value
            mock_app.start = AsyncMock()
            mock_app.stop = AsyncMock()
            rc = await _run(settings, None, logger)
        assert rc == 0

    @patch("shutil.which", return_value="/usr/bin/ffprobe")
    async def test_run_with_config(self, _mock_which, tmp_path):
        from radio_ripper.cli import _run
        from radio_ripper.infra.config import Settings

        logger = logging.getLogger("radio_ripper.test")
        settings = Settings.model_validate({"work_dir": str(tmp_path), "destination": str(tmp_path / "inbox")})
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"log_level": "DEBUG"}))
        loop = asyncio.get_running_loop()
        with (
            patch("radio_ripper.app.RadioRipperApp") as mock_app_cls,
            patch.object(loop, "add_signal_handler"),
            patch("asyncio.Event.wait", new=AsyncMock(return_value=None)),
        ):
            mock_app = mock_app_cls.from_settings_with_live_config.return_value
            mock_app.start = AsyncMock()
            mock_app.stop = AsyncMock()
            rc = await _run(settings, str(config_path), logger)
        assert rc == 0

    @patch("shutil.which", return_value=None)
    async def test_ffprobe_missing_returns_1(self, _mock_which, tmp_path):
        from radio_ripper.cli import _run
        from radio_ripper.infra.config import Settings

        logger = logging.getLogger("radio_ripper.test")
        settings = Settings.model_validate({"work_dir": str(tmp_path), "destination": str(tmp_path / "inbox")})
        rc = await _run(settings, None, logger)
        assert rc == 1
