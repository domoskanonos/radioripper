"""Tests for radio_ripper.infra.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radio_ripper import __version__
from radio_ripper.infra.config import Settings, StreamConfig, load_settings
from radio_ripper.infra.errors import ConfigurationError


def _write_config(tmp_path: Path, payload: dict | str) -> Path:
    p = tmp_path / "config.jsonc"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    p.write_text(text, encoding="utf-8")
    return p


GOOD_BASE = {
    "work_dir": "./recordings",
}


class TestLoadSettings:
    def test_loads_jsonc_comments(self, tmp_path: Path):
        path = _write_config(
            tmp_path,
            "{\n"
            "  // Comment before a property\n"
            '  "work_dir": "./recordings",\n'
            '  "user_agent": "https://example.test//not-a-comment" /* inline comment */\n'
            "}",
        )

        settings = load_settings(path)

        assert settings.work_dir == Path("recordings")
        assert settings.user_agent == "https://example.test//not-a-comment"

    def test_load_good_config(self, tmp_path: Path):
        path = _write_config(tmp_path, GOOD_BASE)
        s = load_settings(path)
        assert isinstance(s, Settings)

    def test_no_keywords_still_valid(self, tmp_path: Path):
        """Config without old discovery settings still loads."""
        cfg = {
            "work_dir": "./recordings",
        }
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.work_dir == Path("recordings")

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
            load_settings(tmp_path / "nonexistent.jsonc")

    def test_no_path_uses_defaults(self):
        """load_settings() without path returns default Settings."""
        s = load_settings()
        assert isinstance(s, Settings)
        assert s.work_dir == Path("./work")
        assert s.destination == Path("./destination")


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
    def test_max_concurrent_streams_has_no_500_upper_limit(self):
        s = Settings.model_validate({**GOOD_BASE, "max_concurrent_streams": 501})

        assert s.max_concurrent_streams == 501

    def test_stream_property(self):
        s = Settings.model_validate(GOOD_BASE)
        ss = s.stream
        assert ss.max_concurrent_streams == 400
        assert ss.user_agent == f"Radio-Ripper/{__version__}"

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
