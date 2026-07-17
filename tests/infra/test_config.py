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
    "destination": "./recordings",
    "database": "./recordings/ripper.db",
    "streams": [{"name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u"}],
}


class TestLoadSettings:
    def test_load_good_config(self, tmp_path: Path):
        path = _write_config(tmp_path, GOOD_BASE)
        s = load_settings(path)
        assert isinstance(s, Settings)
        assert len(s.streams) == 1
        assert str(s.streams[0].url).rstrip("/") == "http://tophits.radiomonster.fm/listen.m3u"

    def test_stream_name_pattern_invalid_chars(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        cfg["streams"] = [{"name": "Top/Hits", "url": "http://x/listen.m3u"}]
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_missing_field_rejected(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        del cfg["streams"]
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_timeout_must_be_positive(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        cfg["read_chunk"] = -1
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_invalid_log_level(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        cfg["log_level"] = "BOGUS"
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_read_chunk_upper_bound(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        cfg["read_chunk"] = 70000
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_invalid_json(self, tmp_path: Path):
        path = _write_config(tmp_path, "{ not json")
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(ConfigurationError):
            load_settings(tmp_path / "nonexistent.json")


class TestDefaults:
    def test_defaults_applied(self):
        s = Settings.model_validate(GOOD_BASE)
        assert s.enrich_metadata is True
        assert s.enrichment_workers == 4
        assert s.read_chunk == 4096
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
