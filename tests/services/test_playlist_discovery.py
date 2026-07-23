"""Tests for radio_ripper.services.playlist_discovery."""

from __future__ import annotations

import json
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
    _download_mega_m3u,
    _filter_keywords,
    _load_cache,
    _parse_m3u_text,
    _probe_icy,
    _save_cache,
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


# ---------------------------------------------------------------------------
# _filter_keywords
# ---------------------------------------------------------------------------


class TestFilterKeywords:
    ENTRIES: ClassVar[list[M3uEntry]] = [
        M3uEntry(name="Classic Rock", url="http://a", source="x"),
        M3uEntry(name="Pop Hits", url="http://b", source="x"),
        M3uEntry(name="Jazz", url="http://c", source="x"),
    ]

    def test_match_keyword(self) -> None:
        result = _filter_keywords(self.ENTRIES, ["rock"])
        assert len(result) == 1
        assert result[0].name == "Classic Rock"

    def test_match_extinf(self) -> None:
        entries = [
            M3uEntry(
                name="Some FM",
                url="http://a",
                source="x",
                extinf='#EXTINF:-1 tvg-id="rock.fm",Rock FM',
            ),
        ]
        result = _filter_keywords(entries, ["rock"])
        assert len(result) == 1

    def test_no_match(self) -> None:
        assert _filter_keywords(self.ENTRIES, ["country"]) == []

    def test_empty_keywords_list(self) -> None:
        result = _filter_keywords(self.ENTRIES, [])
        assert len(result) == 3

    def test_case_insensitive(self) -> None:
        result = _filter_keywords(self.ENTRIES, ["ROCK"])
        assert len(result) == 1

    def test_blank_keywords_skipped(self) -> None:
        result = _filter_keywords(self.ENTRIES, ["", "  "])
        assert len(result) == 3


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
# _probe_icy
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
            result = await _probe_icy("http://example.com/stream")
        assert result["icy"] is True
        assert result["bitrate"] == 128

    @pytest.mark.asyncio
    async def test_no_icy(self) -> None:
        resp = _make_resp(200, {})
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_icy("http://example.com/stream")
        assert result["icy"] is False
        assert result["bitrate"] == 0

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        client = _make_client()
        client.stream.side_effect = httpx.TimeoutException("timed out", request=None)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_icy("http://example.com/stream")
        assert result["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_connect_error(self) -> None:
        client = _make_client()
        client.stream.side_effect = httpx.ConnectError("connection refused")
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_icy("http://example.com/stream")
        assert result["error"] == "connect"

    @pytest.mark.asyncio
    async def test_non_200_status(self) -> None:
        resp = _make_resp(404, {})
        stream_cm = _AsyncCtxMgr(value=resp)
        client = _make_client(stream_cm)
        with patch("httpx.AsyncClient", return_value=client):
            result = await _probe_icy("http://example.com/stream")
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
    async def test_passes_auth_header(self) -> None:
        resp = MagicMock(spec=httpx.Response)
        resp.text = ""
        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await _download_mega_m3u(github_pat="ghp_xyz")
            _, kwargs = mock_client.get.call_args
            actual_headers = kwargs.get("headers", {})
            assert actual_headers.get("Authorization") == "Bearer ghp_xyz"

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
            discovery_enabled=False,
        )
        svc = PlaylistDiscoveryService(settings)
        result = await svc.load_or_discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_uses_cache_when_present(self, tmp_path: Path) -> None:
        stations = [
            StreamConfig(name="Rock FM", url="http://a", icy=True),
        ]
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            discovery_enabled=True,
            temp_dir=tmp_path,
        )
        cf = tmp_path / "discovered_stations.m3u"
        _save_cache(cf, stations)

        svc = PlaylistDiscoveryService(settings)
        result = await svc.load_or_discover()
        assert len(result) == 1
        assert result[0].name == "Rock FM"

    @pytest.mark.asyncio
    async def test_runs_discovery_when_cache_missing(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )
        raw_mega = tmp_path / "---everything-checked-repo.m3u"
        raw_mega.write_text(
            "#EXTM3U\n#EXTINF:-1,Classic Rock\nhttp://rock.example.com\n"
        )

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
        assert (tmp_path / "discovered_stations.m3u").is_file()


# ---------------------------------------------------------------------------
# _discover integration (wired to _download_mega_m3u)
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
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
            discovery_max_stations=5,
        )

        # Mock the download and ICY probe to return OK for the rock station
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
            stations = await svc._discover()

        assert len(stations) == 1
        assert stations[0].name == "Classic Rock"
        assert str(stations[0].url) == "http://rock.example.com/"
        assert stations[0].bitrate == 128
        assert stations[0].icy is True

    @pytest.mark.asyncio
    async def test_no_keyword_match(self, tmp_path: Path) -> None:
        m3u_text = "#EXTM3U\n#EXTINF:-1,Only Jazz\nhttp://jazz.example.com\n"
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            discovery_enabled=True,
            temp_dir=tmp_path,
            stream_keywords=["rock"],
        )

        with patch(
            "radio_ripper.services.playlist_discovery._download_mega_m3u",
            return_value=m3u_text,
        ):
            svc = PlaylistDiscoveryService(settings)
            stations = await svc._discover()

        assert stations == []
