"""Tests for radio_ripper.api.station_api."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radio_ripper.api.config_api import ConfigApi
from radio_ripper.api.station_api import StationApi
from radio_ripper.infra.errors import ConfigurationError


@pytest.fixture
def config_api(tmp_path: Path) -> ConfigApi:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "destination": str(tmp_path / "recordings"),
        "database": str(tmp_path / "songs.db"),
        "streams": [
            {"name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u"},
            {"name": "Rock", "url": "http://rock.radiomonster.fm/listen.m3u"},
        ],
    }), encoding="utf-8")
    api = ConfigApi(cfg)
    api.load()
    return api


class TestStationApi:
    def test_list_stations(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        stations = api.list_stations()
        assert len(stations) == 2
        assert stations[0]["name"] == "TopHits"
        assert stations[1]["name"] == "Rock"

    def test_add_station(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        new_settings = api.add_station("Dance", "http://dance.radiomonster.fm/listen.m3u")
        assert len(new_settings.streams) == 3
        assert new_settings.streams[-1].name == "Dance"

    def test_add_duplicate_raises(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        with pytest.raises(ConfigurationError):
            api.add_station("TopHits", "http://other/listen.m3u")

    def test_add_case_insensitive_dup(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        with pytest.raises(ConfigurationError):
            api.add_station("tophits", "http://other/listen.m3u")

    def test_add_empty_name_raises(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        with pytest.raises(ConfigurationError):
            api.add_station("  ", "http://x/listen")

    def test_edit_station(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        new = api.edit_station("Rock", "Metal", "http://metal.radiomonster.fm/listen.m3u")
        names = [s.name for s in new.streams]
        assert "Metal" in names
        assert "Rock" not in names

    def test_edit_station_not_found(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        with pytest.raises(ConfigurationError):
            api.edit_station("NonExistent", "New", "http://x/listen")

    def test_edit_to_duplicate_name_raises(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        with pytest.raises(ConfigurationError):
            api.edit_station("Rock", "TopHits", "http://x/listen")

    def test_remove_station(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        new = api.remove_station("Rock")
        assert len(new.streams) == 1

    def test_remove_not_found(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        with pytest.raises(ConfigurationError):
            api.remove_station("NonExistent")

    def test_remove_last_raises_after_save(self, config_api: ConfigApi) -> None:
        api = StationApi(config_api)
        s = api.remove_station("Rock")
        config_api.save(s)
        with pytest.raises(ConfigurationError):
            api.remove_station("TopHits")