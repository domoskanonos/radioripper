"""Tests for radio_ripper.api.ripper_api.

Uses a fake HTTP client (no real network) via the StreamRecorder.
The ripper is started in a background thread and stopped quickly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from radio_ripper.api.ripper_api import RipperApi, RipperStatus


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "destination": str(tmp_path / "recordings"),
        "database": str(tmp_path / "songs.db"),
        "streams": [
            {"name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u"},
        ],
    }), encoding="utf-8")
    return cfg


class TestRipperApi:
    def test_initial_state_is_stopped(self) -> None:
        api = RipperApi()
        assert api.status == RipperStatus.STOPPED

    def test_stop_when_stopped_returns_message(self) -> None:
        api = RipperApi()
        msg = api.stop()
        assert "läuft nicht" in msg.lower() or "not running" in msg.lower()

    def test_start_sets_starting(self, tmp_config: Path) -> None:
        from radio_ripper.infra.config import load_settings
        settings = load_settings(tmp_config)
        api = RipperApi()
        msg = api.start(settings)
        assert "gestartet" in msg.lower() or "starting" in msg.lower()
        # give the thread a moment
        time.sleep(0.3)
        # stop it
        msg = api.stop()
        assert "gestoppt" in msg.lower() or "stopped" in msg.lower()
        assert api.status == RipperStatus.STOPPED

    def test_cannot_start_twice(self, tmp_config: Path) -> None:
        from radio_ripper.infra.config import load_settings
        settings = load_settings(tmp_config)
        api = RipperApi()
        api.start(settings)
        time.sleep(0.1)
        # while running (or starting), second start should fail
        msg = api.start(settings)
        assert "bereits" in msg.lower()
        api.stop()