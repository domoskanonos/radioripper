"""Tests for radio_ripper.services.playlist_discovery."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.playlist_discovery import (
    M3uEntry,
    PlaylistDiscoveryService,
    _deduplicate_by_name,
    _distribute_probe_pool,
    _download_mega_m3u,
    _extract_fingerprint,
    _filtered_path,
    _keyword_coverage,
    _load_cache,
    _match_keywords,
    _parse_m3u_text,
    _probe_batch,
    _probe_fingerprint,
    _save_cache,
    _save_prefiltered,
    _selection_fingerprint,
    _work_path,
    probe_icy,
)

# ---------------------------------------------------------------------------
# _parse_m3u_text
# ---------------------------------------------------------------------------


class TestParseM3uText:
    def test_parse_with_extinf(self) -> None:
        text = "#EXTM3U\n#EXTINF:-1,Station Name\nhttp://example.com/stream\n"
        entries = _parse_m3u_text(text, "test.m3u")
        assert len(entries) == 1
        assert entries[0].name == "Station Name"
        assert entries[0].url == "http://example.com/stream"
        assert entries[0].source == "test.m3u"
        assert entries[0].extinf == "#EXTINF:-1,Station Name"

    def test_parse_with_tvg_attr(self) -> None:
        text = '#EXTINF:-1 tvg-id="rock.fm" tvg-name="Rock FM",Rock FM\nhttp://r\n'
        entries = _parse_m3u_text(text, "test.m3u")
        assert len(entries) == 1
        assert entries[0].name == "Rock FM"
        assert entries[0].extinf == '#EXTINF:-1 tvg-id="rock.fm" tvg-name="Rock FM",Rock FM'

    def test_parse_no_extinf_returns_empty(self) -> None:
        text = "http://example.com/stream\n"
        assert _parse_m3u_text(text, "test.m3u") == []

    def test_parse_empty_and_comments(self) -> None:
        text = "#EXTM3U\n\n# some comment\n"
        assert _parse_m3u_text(text, "test.m3u") == []

    def test_parse_multiple_entries(self) -> None:
        text = "#EXTM3U\n#EXTINF:-1,One\nhttp://a\n#EXTINF:-1,Two\nhttp://b\n"
        entries = _parse_m3u_text(text, "test.m3u")
        assert len(entries) == 2
        assert entries[0].name == "One"
        assert entries[1].name == "Two"

    def test_empty_text_returns_empty(self) -> None:
        assert _parse_m3u_text("", "test.m3u") == []


# Filtering is done via _match_keywords + extraction; _filter_keywords was removed
# in favor of _match_keywords.


# ---------------------------------------------------------------------------
# _deduplicate_by_name
# ---------------------------------------------------------------------------


class TestDeduplicateByName:
    def test_removes_duplicates(self) -> None:
        entries = [
            M3uEntry(name="Rock", url="http://a", source="x"),
            M3uEntry(name="Rock", url="http://b", source="x"),
            M3uEntry(name="Pop", url="http://c", source="x"),
        ]
        result = _deduplicate_by_name(entries)
        assert len(result) == 2
        assert result[0].url == "http://a"

    def test_case_insensitive_dedup(self) -> None:
        entries = [
            M3uEntry(name="Rock", url="http://a", source="x"),
            M3uEntry(name="rock", url="http://b", source="x"),
        ]
        assert len(_deduplicate_by_name(entries)) == 1

    def test_empty_name_skipped(self) -> None:
        entries = [
            M3uEntry(name="", url="http://a", source="x"),
            M3uEntry(name="  ", url="http://b", source="x"),
        ]
        assert _deduplicate_by_name(entries) == []


# ---------------------------------------------------------------------------
# probe_icy
# ---------------------------------------------------------------------------


@pytest.fixture
def resp_200():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = {}
    return resp


class _AsyncCtxMgr:
    def __init__(self, value=None, exc=None):
        self._value = value
        self._exc = exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self._value

    async def __aexit__(self, *args):
        pass


def _make_resp(status: int, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.headers = headers or {}
    resp.areceive_headers = AsyncMock()
    return resp


def _make_client(stream_cm=None):
    client = MagicMock(spec=httpx.AsyncClient)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    if stream_cm is not None:
        client.stream.return_value = stream_cm
    return client


class TestProbeIcy:
    @pytest.mark.asyncio
    async def test_icy_stream(self) -> None:
        resp = _make_resp(200, {"icy-metaint": "8192", "icy-br": "128"})
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["icy"] is True
        assert result["bitrate"] == 128

    @pytest.mark.asyncio
    async def test_no_icy(self) -> None:
        resp = _make_resp(200, {})
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["icy"] is False
        assert result["bitrate"] == 0

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        client = _make_client()
        client.stream.side_effect = httpx.TimeoutException("timed out", request=None)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_connect_error(self) -> None:
        client = _make_client()
        client.stream.side_effect = httpx.ConnectError("connection refused")
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["error"] == "connect"

    @pytest.mark.asyncio
    async def test_non_200_status(self) -> None:
        resp = _make_resp(404, {})
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["error"] == "HTTP 404"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


class TestCacheHelpers:
    def test_cache_roundtrip(self, tmp_path: Path) -> None:
        stations = [
            StreamConfig(name="Rock FM", url="http://a", icy=True),
            StreamConfig(name="Pop FM", url="http://b", icy=True),
        ]
        cf = tmp_path / "cache.json"
        _save_cache(cf, stations)
        loaded, kh = _load_cache(cf)
        assert len(loaded) == 2
        assert loaded[0].name == "Rock FM"
        assert kh == ""

    def test_load_legacy_flat_list(self, tmp_path: Path) -> None:
        data = [
            {"name": "A", "url": "http://a", "icy": True},
            {"name": "B", "url": "http://b", "icy": False},
        ]
        cf = tmp_path / "cache.json"
        cf.write_text(json.dumps(data))
        loaded, kh = _load_cache(cf)
        assert len(loaded) == 1
        assert loaded[0].name == "A"
        assert kh == ""

    def test_load_corrupt_cache(self, tmp_path: Path) -> None:
        cf = tmp_path / "cache.json"
        cf.write_text("not json")
        loaded, kh = _load_cache(cf)
        assert loaded == []
        assert kh == ""


# ---------------------------------------------------------------------------
# _download_mega_m3u
# ---------------------------------------------------------------------------


class TestDownloadMegaM3u:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        content = "#EXTM3U\n#EXTINF:-1,Rock FM\nhttp://a\n"
        resp = MagicMock(spec=httpx.Response)
        resp.text = content
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            text = await _download_mega_m3u()
        assert text == content

    @pytest.mark.asyncio
    async def test_http_error(self) -> None:
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )
        )

        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(httpx.HTTPStatusError),
        ):
            await _download_mega_m3u()


# ---------------------------------------------------------------------------
# PlaylistDiscoveryService
# ---------------------------------------------------------------------------


class TestPlaylistDiscoveryService:
    @pytest.mark.asyncio
    async def test_discovery_not_enabled(self) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir="./rec",
            discovery_enabled=False,
        )
        svc = PlaylistDiscoveryService(settings)
        result = await svc.load_or_discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_uses_cache_when_present(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            max_concurrent_streams=1,
            stream_keywords=["rock"],
        )
        stations_in = [
            StreamConfig(name="Rock FM", url="http://a", icy=True),
        ]
        _save_cache(_work_path(settings), stations_in)

        svc = PlaylistDiscoveryService(settings)
        result = await svc.load_or_discover()
        assert len(result) == 1
        assert result[0].name == "Rock FM"

    @pytest.mark.asyncio
    async def test_runs_discovery_when_cache_missing(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )
        raw_mega = tmp_path / "---everything-checked-repo.m3u"
        raw_mega.write_text("#EXTM3U\n#EXTINF:-1,Classic Rock\nhttp://rock.example.com\n")

        mock_entry = M3uEntry(name="Classic Rock", url="http://rock.example.com", source="mega.m3u")
        mock_probe = {"icy": True, "bitrate": 128}

        with patch(
            "radio_ripper.services.playlist_discovery._probe_batch",
            return_value=[(mock_entry, mock_probe)],
        ):
            svc = PlaylistDiscoveryService(settings)
            result = await svc.load_or_discover()

        assert len(result) == 1
        assert result[0].name == "Classic Rock"
        assert (tmp_path / "filtered_checked_stations.m3u").is_file()
        assert (tmp_path / "work_stations.m3u").is_file()


# ---------------------------------------------------------------------------
# Config fingerprints
# ---------------------------------------------------------------------------


class _SettingsBuilder:
    def __call__(self, tmp_path: Path, **overrides) -> Settings:
        base = {
            "destination": "./rec",
            "work_dir": tmp_path,
            "discovery_enabled": True,
        }
        base.update(overrides)
        return Settings(**base)


class TestConfigFingerprint:
    def _settings(self, tmp_path: Path, **overrides) -> Settings:
        return _SettingsBuilder()(tmp_path, **overrides)

    def test_selection_fingerprint_sensitive_to_config(self, tmp_path) -> None:
        a = self._settings(tmp_path, max_concurrent_streams=100, stream_keywords=["rock"])
        b = self._settings(tmp_path, max_concurrent_streams=200, stream_keywords=["rock"])
        c = self._settings(tmp_path, max_concurrent_streams=100, stream_keywords=["rock", "pop"])
        assert _selection_fingerprint(a) != _selection_fingerprint(b)
        assert _selection_fingerprint(a) != _selection_fingerprint(c)

    def test_probe_fingerprint_sensitive_to_bitrate(self, tmp_path) -> None:
        a = self._settings(tmp_path, discovery_min_bitrate=0)
        b = self._settings(tmp_path, discovery_min_bitrate=128)
        assert _probe_fingerprint(a) != _probe_fingerprint(b)

    def test_extract_fingerprint_from_m3u(self, tmp_path) -> None:
        fp = _selection_fingerprint(self._settings(tmp_path))
        text = "#EXTM3U\n# radio-ripper-config: " + fp + "\n#EXTINF:-1,X\nhttp://x\n"
        assert _extract_fingerprint(text) == fp

    def test_extract_fingerprint_empty_when_missing(self) -> None:
        assert _extract_fingerprint("#EXTM3U\n#EXTINF:-1,X\nhttp://x\n") == ""
        assert _extract_fingerprint("") == ""


class TestCacheFingerprintInvalidation:
    def _settings(self, tmp_path: Path, **overrides) -> Settings:
        return _SettingsBuilder()(tmp_path, **overrides)

    @pytest.mark.asyncio
    async def test_work_cache_stale_rebuilds_from_filtered(self, tmp_path) -> None:
        settings_a = self._settings(tmp_path, max_concurrent_streams=5, stream_keywords=[])
        work = _work_path(settings_a)
        _save_cache(
            work,
            [StreamConfig(name="Old", url="http://old", icy=True)],
            _selection_fingerprint(settings_a),
        )

        settings_b = self._settings(tmp_path, max_concurrent_streams=10, stream_keywords=[])
        _save_prefiltered(
            _filtered_path(settings_b),
            [(M3uEntry(name="New A", url="http://a", source="f"), {"icy": True, "bitrate": 128})],
            _probe_fingerprint(settings_b),
        )

        svc = PlaylistDiscoveryService(settings_b)
        result = await svc.load_or_discover()

        assert [s.name for s in result] == ["New A"]
        loaded, fp = _load_cache(work)
        assert fp == _selection_fingerprint(settings_b)
        assert loaded[0].name == "New A"

    @pytest.mark.asyncio
    async def test_filtered_cache_stale_triggers_reprobe(self, tmp_path) -> None:
        settings_a = self._settings(tmp_path, discovery_min_bitrate=0)
        filtered = _filtered_path(settings_a)
        _save_prefiltered(
            filtered,
            [(M3uEntry(name="Rock", url="http://a", source="f"), {"icy": True, "bitrate": 128})],
            _probe_fingerprint(settings_a),
        )

        settings_b = self._settings(tmp_path, discovery_min_bitrate=200)
        m3u_text = "#EXTM3U\n#EXTINF:-1,New Rock\nhttp://new.example.com\n"
        new_entry = M3uEntry(name="New Rock", url="http://new.example.com", source="mega")
        svc = PlaylistDiscoveryService(settings_b)
        with (
            patch(
                "radio_ripper.services.playlist_discovery._download_mega_m3u",
                return_value=m3u_text,
            ),
            patch(
                "radio_ripper.services.playlist_discovery._probe_batch",
                return_value=[(new_entry, {"icy": True, "bitrate": 256})],
            ) as probe,
        ):
            result = await svc.load_or_discover()

        probe.assert_awaited_once()
        assert [s.name for s in result] == ["New Rock"]
        text = filtered.read_text("utf-8")
        assert _extract_fingerprint(text) == _probe_fingerprint(settings_b)

    @pytest.mark.asyncio
    async def test_legacy_work_cache_without_fingerprint_still_used(self, tmp_path) -> None:
        settings = self._settings(tmp_path, max_concurrent_streams=5, stream_keywords=["rock"])
        _save_cache(_work_path(settings), [StreamConfig(name="Legacy", url="http://a", icy=True)])

        svc = PlaylistDiscoveryService(settings)
        result = await svc.load_or_discover()

        assert [s.name for s in result] == ["Legacy"]


# ---------------------------------------------------------------------------
# load_or_discover integration (wired to _download_mega_m3u)
# ---------------------------------------------------------------------------


class TestDiscover:
    @pytest.mark.asyncio
    async def test_full_flow(self, tmp_path: Path) -> None:
        m3u_text = (
            "#EXTM3U\n"
            "#EXTINF:-1,Classic Rock\nhttp://rock.example.com\n"
            "#EXTINF:-1,Pop Hits\nhttp://pop.example.com\n"
            "#EXTINF:-1,Jazz Cafe\nhttp://jazz.example.com\n"
        )
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
            max_concurrent_streams=5,
        )

        mock_entry = M3uEntry(name="Classic Rock", url="http://rock.example.com", source="mega.m3u")
        mock_probe = {"icy": True, "bitrate": 128}

        with (
            patch(
                "radio_ripper.services.playlist_discovery._download_mega_m3u",
                return_value=m3u_text,
            ),
            patch(
                "radio_ripper.services.playlist_discovery._probe_batch",
                return_value=[(mock_entry, mock_probe)],
            ),
        ):
            svc = PlaylistDiscoveryService(settings)
            stations = await svc.load_or_discover()

        assert len(stations) == 1
        assert stations[0].name == "Classic Rock"
        assert str(stations[0].url) == "http://rock.example.com/"
        assert stations[0].bitrate == 128
        assert stations[0].icy is True
        assert (tmp_path / "random_stations.m3u").is_file()
        assert (tmp_path / "filtered_checked_stations.m3u").is_file()
        assert (tmp_path / "work_stations.m3u").is_file()

    @pytest.mark.asyncio
    async def test_no_keyword_match(self, tmp_path: Path) -> None:
        m3u_text = "#EXTM3U\n#EXTINF:-1,Only Jazz\nhttp://jazz.example.com\n"
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )

        with patch(
            "radio_ripper.services.playlist_discovery._download_mega_m3u",
            return_value=m3u_text,
        ):
            svc = PlaylistDiscoveryService(settings)
            stations = await svc.load_or_discover()

        assert stations == []


# ---------------------------------------------------------------------------
# _match_keywords
# ---------------------------------------------------------------------------


class TestMatchKeywords:
    ENTRIES: ClassVar[list[M3uEntry]] = [
        M3uEntry(name="Classic Rock", url="http://a", source="x"),
        M3uEntry(name="Pop Hits", url="http://b", source="x"),
        M3uEntry(name="Jazz Cafe", url="http://c", source="x", extinf="#EXTINF:-1,Jazz Cafe"),
    ]

    def test_empty_keywords_all_returned(self):
        result = _match_keywords(self.ENTRIES, [])
        assert len(result) == 3
        for _, matched in result:
            assert matched == set()

    def test_only_blank_keywords_all_returned(self):
        result = _match_keywords(self.ENTRIES, ["", "  "])
        assert len(result) == 3

    def test_match_single_keyword(self):
        result = _match_keywords(self.ENTRIES, ["rock"])
        assert len(result) == 1
        entry, matched = result[0]
        assert entry.name == "Classic Rock"
        assert matched == {"rock"}

    def test_match_multiple_keywords_in_one_entry(self):
        entries = [
            M3uEntry(name="Classic Rock Pop", url="http://a", source="x"),
        ]
        result = _match_keywords(entries, ["rock", "pop"])
        assert len(result) == 1
        _, matched = result[0]
        assert matched == {"rock", "pop"}

    def test_match_from_extinf_only(self):
        entries = [
            M3uEntry(name="Some FM", url="http://a", source="x", extinf='#EXTINF:-1 tvg-id="rock.fm"'),
        ]
        result = _match_keywords(entries, ["rock"])
        assert len(result) == 1

    def test_no_match_returns_empty(self):
        result = _match_keywords(self.ENTRIES, ["country"])
        assert result == []


# ---------------------------------------------------------------------------
# _distribute_probe_pool
# ---------------------------------------------------------------------------


class TestDistributeProbePool:
    def _make_match(self, name: str, kw: str, url: str = "http://a") -> tuple[M3uEntry, set[str]]:
        return M3uEntry(name=name, url=url, source="test"), {kw}

    def test_no_keywords_returns_all(self):
        entry = M3uEntry(name="Rock", url="http://a", source="test")
        result = _distribute_probe_pool([(entry, {"rock"})], [], 10)
        assert result == [entry]

    def test_max_needed_zero_returns_all(self):
        entry = M3uEntry(name="Rock", url="http://a", source="test")
        result = _distribute_probe_pool([(entry, {"rock"})], ["rock"], 0)
        assert result == [entry]

    def test_single_keyword_selects_up_to_max(self):
        entries = [M3uEntry(name=f"Rock {i}", url=f"http://{i}", source="test") for i in range(5)]
        matched = [(e, {"rock"}) for e in entries]
        result = _distribute_probe_pool(matched, ["rock"], 3)
        assert len(result) == 3

    def test_multiple_keywords_round_robin(self):
        rock_entries = [M3uEntry(name=f"Rock {i}", url=f"http://r{i}", source="test") for i in range(3)]
        jazz_entries = [M3uEntry(name=f"Jazz {i}", url=f"http://j{i}", source="test") for i in range(3)]
        matched = [(e, {"rock"}) for e in rock_entries] + [(e, {"jazz"}) for e in jazz_entries]
        result = _distribute_probe_pool(matched, ["rock", "jazz"], 4)
        assert len(result) == 4
        rock_in_pool = sum(1 for e in result if "Rock" in e.name)
        jazz_in_pool = sum(1 for e in result if "Jazz" in e.name)
        assert rock_in_pool >= 1
        assert jazz_in_pool >= 1

    def test_keyword_exhausted_continues_round_robin(self):
        rock_entries = [M3uEntry(name="Rock Only", url="http://r0", source="test")]
        jazz_entries = [M3uEntry(name=f"Jazz {i}", url=f"http://j{i}", source="test") for i in range(5)]
        matched = [(e, {"rock"}) for e in rock_entries] + [(e, {"jazz"}) for e in jazz_entries]
        result = _distribute_probe_pool(matched, ["rock", "jazz"], 4)
        assert len(result) == 4
        assert result[0].name == "Rock Only"

    def test_runs_out_of_entries(self):
        entries = [M3uEntry(name="Rock", url="http://a", source="test")]
        matched = [(e, {"rock"}) for e in entries]
        result = _distribute_probe_pool(matched, ["rock", "jazz"], 10)
        assert len(result) == 1

    def test_warns_when_fewer_than_5_per_keyword(self, caplog):
        caplog.set_level(logging.WARNING, logger="radio_ripper.discovery")
        entries = [M3uEntry(name=f"Rock {i}", url=f"http://{i}", source="test") for i in range(3)]
        matched = [(e, {"rock"}) for e in entries]
        _distribute_probe_pool(matched, ["rock"], 5)
        assert "has only 3 station(s) in probe pool" in caplog.text


# ---------------------------------------------------------------------------
# _keyword_coverage
# ---------------------------------------------------------------------------


class TestKeywordCoverage:
    def _make_good(self, name: str, kw: str) -> tuple[M3uEntry, dict]:
        return (
            M3uEntry(name=name, url="http://a", source="test", extinf=f'tvg-name="{kw}"'),
            {"icy": True, "bitrate": 128},
        )

    def test_warning_fewer_than_5(self, caplog):
        caplog.set_level(logging.WARNING, logger="radio_ripper.discovery")
        good = [self._make_good("Rock FM", "rock")]
        _keyword_coverage(good, ["rock"])
        assert "has only 1 probed station(s) (< 5)" in caplog.text

    def test_info_5_or_more(self, caplog):
        caplog.set_level(logging.INFO, logger="radio_ripper.discovery")
        good = [self._make_good(f"Rock {i}", "rock") for i in range(5)]
        _keyword_coverage(good, ["rock"])
        assert "5 stations" in caplog.text


# ---------------------------------------------------------------------------
# probe_icy — additional edge cases
# ---------------------------------------------------------------------------


class TestProbeIcyEdgeCases:
    @pytest.mark.asyncio
    async def test_read_chunk_raises(self):
        async def _aiter_error():
            raise RuntimeError("connection lost")
            yield  # pragma: no cover

        resp = _make_resp(200, {"icy-metaint": "8192"})
        resp.aiter_bytes = _aiter_error
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["error"] == "no data: connection lost"

    @pytest.mark.asyncio
    async def test_remote_protocol_error(self):
        client = _make_client()
        client.stream.side_effect = httpx.RemoteProtocolError("protocol error")
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["error"] == "protocol"

    @pytest.mark.asyncio
    async def test_generic_exception(self):
        client = _make_client()
        client.stream.side_effect = RuntimeError("something unexpected")
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["error"] == "something unexpected"

    @pytest.mark.asyncio
    async def test_non_http_status_206_accepted(self):
        resp = _make_resp(206, {"icy-metaint": "8192"})
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["icy"] is True

    @pytest.mark.asyncio
    async def test_read_chunk_yields_data(self):
        async def _aiter_one():
            yield b"some data"

        resp = _make_resp(200, {"icy-metaint": "8192"})
        resp.aiter_bytes = _aiter_one
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await probe_icy("http://example.com/stream")
        assert result["icy"] is True
        assert result["read_bytes"] == 9


# ---------------------------------------------------------------------------
# _probe_batch
# ---------------------------------------------------------------------------


class TestProbeBatch:
    @pytest.mark.asyncio
    async def test_all_icy(self):
        entries = [
            M3uEntry(name="Rock", url="http://a", source="test"),
            M3uEntry(name="Jazz", url="http://b", source="test"),
        ]
        with patch(
            "radio_ripper.services.playlist_discovery.probe_icy",
            new_callable=AsyncMock,
            side_effect=[
                {"icy": True, "bitrate": 128, "error": None},
                {"icy": True, "bitrate": 256, "error": None},
            ],
        ):
            sem = asyncio.Semaphore(50)
            results = await _probe_batch(entries, 10, sem)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mixed_icy(self):
        entries = [
            M3uEntry(name="Rock", url="http://a", source="test"),
            M3uEntry(name="Jazz", url="http://b", source="test"),
        ]
        with patch(
            "radio_ripper.services.playlist_discovery.probe_icy",
            new_callable=AsyncMock,
            side_effect=[
                {"icy": True, "bitrate": 128, "error": None},
                {"icy": False, "bitrate": 0, "error": None},
            ],
        ):
            sem = asyncio.Semaphore(50)
            results = await _probe_batch(entries, 10, sem)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_task_exception_skipped(self):
        entries = [
            M3uEntry(name="Rock", url="http://a", source="test"),
        ]
        with patch(
            "radio_ripper.services.playlist_discovery.probe_icy",
            new_callable=AsyncMock,
            side_effect=RuntimeError("probe failed"),
        ):
            sem = asyncio.Semaphore(50)
            results = await _probe_batch(entries, 10, sem)
        assert results == []

    @pytest.mark.asyncio
    async def test_max_ok_cancels_remaining(self):
        call_count = 0

        async def _staggered(url: str, **kw: object) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return {"icy": True, "bitrate": 128, "error": None}
            await asyncio.sleep(100)
            return {"icy": True, "bitrate": 128}  # pragma: no cover

        entries = [M3uEntry(name=f"S{i}", url=f"http://{i}", source="test") for i in range(3)]
        with patch("radio_ripper.services.playlist_discovery.probe_icy", _staggered):
            sem = asyncio.Semaphore(50)
            results = await _probe_batch(entries, 2, sem)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# _load_cache — additional edge cases
# ---------------------------------------------------------------------------


class TestLoadCacheEdgeCases:
    def test_legacy_json_not_a_list_falls_to_m3u(self, tmp_path: Path) -> None:
        cf = tmp_path / "cache.json"
        cf.write_text("[1, 2, 3]")
        loaded, _kh = _load_cache(cf)
        assert loaded == []

    def test_legacy_json_station_creation_fails_falls_to_m3u(self, tmp_path: Path) -> None:
        cf = tmp_path / "cache.json"
        cf.write_text(json.dumps([{"name": "Bad", "url": "not-a-url", "icy": True}]))
        loaded, _kh = _load_cache(cf)
        assert loaded == []

    def test_m3u_entry_creation_skipped(self, tmp_path: Path) -> None:
        cf = tmp_path / "cache.json"
        cf.write_text("#EXTM3U\n#EXTINF:-1,Bad URL\nnot-a-valid-url\n")
        loaded, _kh = _load_cache(cf)
        assert loaded == []

    def test_cache_file_not_found(self, tmp_path: Path) -> None:
        cf = tmp_path / "nonexistent.json"
        loaded, _kh = _load_cache(cf)
        assert loaded == []


# ---------------------------------------------------------------------------
# PlaylistDiscoveryService — additional edge cases
# ---------------------------------------------------------------------------


class TestPlaylistDiscoveryServiceEdgeCases:
    @pytest.mark.asyncio
    async def test_load_or_discover_cache_empty_runs_discovery(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
        )
        pf = tmp_path / "prefiltered.m3u"
        pf.write_text("#EXTM3U\n")
        with patch(
            "radio_ripper.services.playlist_discovery._download_mega_m3u",
            return_value="#EXTM3U\n",
        ):
            svc = PlaylistDiscoveryService(settings)
            result = await svc.load_or_discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_load_or_discover_download_save_fails_gracefully(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )
        m3u_text = "#EXTM3U\n#EXTINF:-1,Classic Rock\nhttp://rock.example.com\n"
        mock_entry = M3uEntry(name="Classic Rock", url="http://rock.example.com", source="mega.m3u")
        with (
            patch(
                "radio_ripper.services.playlist_discovery._download_mega_m3u",
                return_value=m3u_text,
            ),
            patch(
                "radio_ripper.services.playlist_discovery._probe_batch",
                return_value=[(mock_entry, {"icy": True, "bitrate": 128})],
            ),
            patch.object(Path, "write_text", side_effect=OSError("read-only")),
            patch(
                "radio_ripper.services.playlist_discovery._save_prefiltered",
            ),
        ):
            svc = PlaylistDiscoveryService(settings)
            result = await svc.load_or_discover()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_load_or_discover_no_stations_after_discover(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )
        with patch(
            "radio_ripper.services.playlist_discovery._download_mega_m3u",
            return_value="#EXTM3U\n#EXTINF:-1,Other\nhttp://other\n",
        ):
            svc = PlaylistDiscoveryService(settings)
            result = await svc.load_or_discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_discover_raw_mega_read_fails_downloads(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )
        raw_mega = tmp_path / "---everything-checked-repo.m3u"
        raw_mega.write_text("ignored")
        m3u_text = "#EXTM3U\n"
        svc = PlaylistDiscoveryService(settings)
        with (
            patch("pathlib.Path.read_text", side_effect=OSError("denied")),
            patch(
                "radio_ripper.services.playlist_discovery._download_mega_m3u",
                return_value=m3u_text,
            ),
            patch.object(svc, "_probe_and_filter", return_value=[]),
        ):
            await svc.load_or_discover()

    @pytest.mark.asyncio
    async def test_discover_save_raw_mega_fails_gracefully(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )
        m3u_text = "#EXTM3U\n"
        svc = PlaylistDiscoveryService(settings)
        with (
            patch(
                "radio_ripper.services.playlist_discovery._download_mega_m3u",
                return_value=m3u_text,
            ),
            patch("pathlib.Path.write_text", side_effect=OSError("read-only")),
            patch.object(svc, "_probe_and_filter", return_value=[]),
        ):
            await svc.load_or_discover()

    @pytest.mark.asyncio
    async def test_discover_raw_mega_exists_reads_it(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
        )
        raw_mega = tmp_path / "---everything-checked-repo.m3u"
        m3u_text = "#EXTM3U\n"
        raw_mega.write_text(m3u_text)
        svc = PlaylistDiscoveryService(settings)
        with patch.object(svc, "_probe_and_filter", return_value=[]):
            await svc.load_or_discover()

    @pytest.mark.asyncio
    async def test_probe_and_filter_bitrate_filter(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            discovery_min_bitrate=200,
        )
        entries = [
            M3uEntry(name="Classic Rock", url="http://rock.example.com", source="test"),
            M3uEntry(name="Pop Hits", url="http://pop.example.com", source="test"),
        ]
        mock_results = [
            (entries[0], {"icy": True, "bitrate": 128}),
            (entries[1], {"icy": True, "bitrate": 256}),
        ]
        with patch(
            "radio_ripper.services.playlist_discovery._probe_batch",
            return_value=mock_results,
        ):
            svc = PlaylistDiscoveryService(settings)
            result = await svc._probe_and_filter(entries)
        assert len(result) == 1
        assert result[0][0].name == "Pop Hits"

    def test_select_from_prefiltered_skips_invalid_entry(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
            max_concurrent_streams=10,
        )
        good = [
            (
                M3uEntry(name="Classic Rock", url="not-a-valid-url", source="mega.m3u"),
                {"icy": True, "bitrate": 128},
            ),
        ]
        svc = PlaylistDiscoveryService(settings)
        stations = svc._select_from_prefiltered(good)
        assert stations == []

    @pytest.mark.asyncio
    async def test_probe_and_filter_empty_entries(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            work_dir=tmp_path,
            discovery_enabled=True,
            temp_dir=tmp_path,
        )
        svc = PlaylistDiscoveryService(settings)
        result = await svc._probe_and_filter([])
        assert result == []
