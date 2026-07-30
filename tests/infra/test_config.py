"""Tests for radio_ripper.infra.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radio_ripper.infra.config import Settings, StreamConfig, load_settings
from radio_ripper.infra.errors import ConfigurationError


def _write_config(tmp_path: Path, payload: dict | str) -> Path:
    p = tmp_path / "config.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    p.write_text(text, encoding="utf-8")
    return p


GOOD_BASE = {
    "work_dir": "./recordings",
    "streams": [{"name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u"}],
}


class TestLoadSettings:
    def test_load_good_config(self, tmp_path: Path):
        path = _write_config(tmp_path, GOOD_BASE)
        s = load_settings(path)
        assert isinstance(s, Settings)
        assert len(s.streams) == 1
        assert str(s.streams[0].url).rstrip("/") == "http://tophits.radiomonster.fm/listen.m3u"

    def test_missing_streams_defaults_to_empty(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        del cfg["streams"]
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.streams == []

    def test_no_keywords_and_no_streams_still_valid(self, tmp_path: Path):
        """Empty streams + empty keywords is valid; discovery handles the rest."""
        cfg = {
            "work_dir": "./recordings",
            "stream_keywords": [],
            "discovery_enabled": False,
        }
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.streams == []
        assert s.stream_keywords == []
        assert s.discovery_enabled is False

    def test_invalid_log_level(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        cfg["log_level"] = "BOGUS"
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_invalid_json(self, tmp_path: Path):
        path = _write_config(tmp_path, "{ not json")
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_missing_file_with_path(self, tmp_path: Path):
        """Explicit --config for a non-existent file raises."""
        with pytest.raises(ConfigurationError):
            load_settings(tmp_path / "nonexistent.json")

    def test_no_path_uses_defaults(self):
        """load_settings() without path returns default Settings."""
        s = load_settings()
        assert isinstance(s, Settings)
        assert s.work_dir == Path("./work")
        assert s.destination == Path("/home/laptop/trash/mp3")


class TestDefaults:
    def test_defaults_applied(self):
        s = Settings.model_validate(GOOD_BASE)
        assert s.request_timeout == 30.0
        assert s.log_level == "INFO"


class TestStreamConfig:
    def test_accepts_simple_name(self):
        StreamConfig(name="TopHits", url="http://x/listen.m3u")

    def test_rejects_empty_name(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            StreamConfig(name="  ", url="http://x/listen.m3u")

    def test_accepts_spaces_and_dashes(self):
        c = StreamConfig(name="Top-Hits FM", url="http://x/listen.m3u")
        assert c.name == "Top-Hits FM"


class TestSettingsProperties:
    def test_stream_property(self):
        s = Settings.model_validate(GOOD_BASE)
        ss = s.stream
        assert ss.max_concurrent_streams == 400
        assert ss.user_agent == "Radio-Ripper/2.0"

    def test_discovery_property(self):
        s = Settings.model_validate(GOOD_BASE)
        ds = s.discovery
        assert ds.discovery_enabled is True
        assert ds.stream_keywords != []

    def test_log_level_critical_valid(self):
        s = Settings.model_validate({**GOOD_BASE, "log_level": "CRITICAL"})
        assert s.log_level == "CRITICAL"


class TestLiveConfig:
    async def test_check_reload_no_change(self, tmp_path):
        from radio_ripper.infra.config import LiveConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(GOOD_BASE))
        initial = load_settings(cfg_file)
        lc = LiveConfig(cfg_file, initial)
        diff = await lc.check_reload()
        assert diff == {}

    async def test_check_reload_detects_change(self, tmp_path):
        from radio_ripper.infra.config import LiveConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({**GOOD_BASE, "log_level": "INFO"}))
        initial = load_settings(cfg_file)
        lc = LiveConfig(cfg_file, initial)
        cfg_file.write_text(json.dumps({**GOOD_BASE, "log_level": "DEBUG"}))
        diff = await lc.check_reload()
        assert "log_level" in diff
        assert diff["log_level"] == ("INFO", "DEBUG")

    async def test_check_reload_file_deleted(self, tmp_path):
        from radio_ripper.infra.config import LiveConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(GOOD_BASE))
        initial = load_settings(cfg_file)
        lc = LiveConfig(cfg_file, initial)
        cfg_file.unlink()
        diff = await lc.check_reload()
        assert diff == {}

    async def test_check_reload_invalid_json(self, tmp_path):
        from radio_ripper.infra.config import LiveConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(GOOD_BASE))
        initial = load_settings(cfg_file)
        lc = LiveConfig(cfg_file, initial)
        cfg_file.write_text("not valid json")
        diff = await lc.check_reload()
        assert diff == {}

    def test_settings_property(self, tmp_path):
        from radio_ripper.infra.config import LiveConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(GOOD_BASE))
        initial = load_settings(cfg_file)
        lc = LiveConfig(cfg_file, initial)
        assert lc.settings is initial
        assert lc.path == cfg_file

    async def test_check_reload_preserves_unchanged(self, tmp_path):
        from radio_ripper.infra.config import LiveConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({**GOOD_BASE, "max_concurrent_streams": 100}))
        initial = load_settings(cfg_file)
        lc = LiveConfig(cfg_file, initial)
        cfg_file.write_text(json.dumps({**GOOD_BASE, "max_concurrent_streams": 200}))
        diff = await lc.check_reload()
        assert "max_concurrent_streams" in diff
        assert diff["max_concurrent_streams"] == (100, 200)
        assert "log_level" not in diff
