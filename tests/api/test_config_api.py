"""Tests for radio_ripper.api.config_api."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radio_ripper.api.config_api import ConfigApi
from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.infra.errors import ConfigurationError


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    data = {
        "destination": str(tmp_path / "recordings"),
        "database": str(tmp_path / "songs.db"),
        "streams": [
            {"name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u"},
        ],
        "log_level": "INFO",
    }
    cfg.write_text(json.dumps(data), encoding="utf-8")
    return cfg


class TestConfigApi:
    def test_load_returns_valid_settings(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        settings = api.load()
        assert isinstance(settings, Settings)
        assert len(settings.streams) == 1
        assert settings.streams[0].name == "TopHits"

    def test_load_caches_settings(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        s1 = api.load()
        s2 = api.settings()
        assert s1 is s2

    def test_settings_auto_loads(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        settings = api.settings()
        assert settings.streams[0].name == "TopHits"

    def test_reload_refetches(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        s1 = api.load()
        # overwrite config
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        data["log_level"] = "DEBUG"
        tmp_config.write_text(json.dumps(data), encoding="utf-8")
        s2 = api.reload()
        assert s2.log_level == "DEBUG"
        assert s1.log_level == "INFO"

    def test_save_writes_json(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        s = api.load()
        s2 = s.model_copy(update={"log_level": "WARNING"})
        api.save(s2)
        raw = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert raw["log_level"] == "WARNING"

    def test_update_field_returns_new_settings(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        new = api.update_field("log_level", "ERROR")
        assert new.log_level == "ERROR"
        # original unchanged
        assert api.settings().log_level == "INFO"

    def test_update_field_unknown_key_raises(self, tmp_config: Path) -> None:
        api = ConfigApi(tmp_config)
        with pytest.raises(KeyError):
            api.update_field("nonexistent_field", 42)

    def test_make_stream_validates(self) -> None:
        stream = ConfigApi.make_stream("Rock", "http://x/listen.m3u")
        assert stream.name == "Rock"

    def test_default_settings_has_one_stream(self) -> None:
        s = ConfigApi.default_settings()
        assert isinstance(s, Settings)
        assert len(s.streams) == 1
        assert isinstance(s.streams[0], StreamConfig)

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        api = ConfigApi(tmp_path / "nonexistent.json")
        with pytest.raises(ConfigurationError):
            api.load()
