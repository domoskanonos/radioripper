"""Tests for radio_ripper.services.playlist_discovery."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.services.playlist_discovery import (
    M3uEntry,
    PlaylistDiscoveryService,
    _deduplicate_by_name,
    _filter_keywords,
    _git_sparse_checkout,
    _is_cache_fresh,
    _keywords_hash,
    _load_cache,
    _parse_checked_dir,
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

    def test_parse_with_tvg_attr(self) -> None:
        text = '#EXTINF:-1 tvg-id="rock.fm" tvg-name="Rock FM",Rock FM\nhttp://r\n'
        entries = _parse_m3u_text(text, "test.m3u")
        assert len(entries) == 1
        assert entries[0].name == "Rock FM"

    def test_parse_no_extinf_returns_empty(self) -> None:
        text = "http://example.com/stream\n"
        assert _parse_m3u_text(text, "test.m3u") == []

    def test_parse_empty_and_comments(self) -> None:
        text = "#EXTM3U\n\n# some comment\n"
        assert _parse_m3u_text(text, "test.m3u") == []

    def test_parse_multiple_entries(self) -> None:
        text = (
            "#EXTM3U\n"
            "#EXTINF:-1,One\nhttp://a\n"
            "#EXTINF:-1,Two\nhttp://b\n"
        )
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
    ENTRIES = [
        M3uEntry(name="Classic Rock", url="http://a", source="x"),
        M3uEntry(name="Pop Hits", url="http://b", source="x"),
        M3uEntry(name="Jazz", url="http://c", source="x"),
    ]

    def test_match_keyword(self) -> None:
        result = _filter_keywords(self.ENTRIES, ["rock"])
        assert len(result) == 1
        assert result[0].name == "Classic Rock"

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
        assert result[0].url == "http://a"  # first occurrence

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
    """Reusable async context manager that yields a fixed value or raises."""

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
    """Build a mocked AsyncClient suitable for ``async with client as c:``."""
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
    def test_is_cache_fresh_when_file_recent(self, tmp_path: Path) -> None:
        cf = tmp_path / "cache.json"
        cf.write_text("{}")
        assert _is_cache_fresh(cf, max_age_days=7) is True

    def test_is_cache_stale(self, tmp_path: Path) -> None:
        import os
        cf = tmp_path / "cache.json"
        cf.write_text("{}")
        old = time.time() - 8 * 86400
        os.utime(cf, (old, old))
        assert _is_cache_fresh(cf, max_age_days=7) is False

    def test_is_cache_missing(self, tmp_path: Path) -> None:
        assert _is_cache_fresh(tmp_path / "nope.json", max_age_days=7) is False

    def test_cache_roundtrip(self, tmp_path: Path) -> None:
        stations = [
            StreamConfig(name="Rock FM", url="http://a", icy=True),
            StreamConfig(name="Pop FM", url="http://b", icy=True),
        ]
        cf = tmp_path / "cache.json"
        _save_cache(cf, stations, keywords_hash="abc")
        loaded, kh = _load_cache(cf)
        assert len(loaded) == 2
        assert loaded[0].name == "Rock FM"
        assert kh == "abc"

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
    async def test_uses_cache_when_fresh(self, tmp_path: Path) -> None:
        stations = [
            StreamConfig(name="Rock FM", url="http://a", icy=True),
        ]
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            discovery_enabled=True,
            reprobe_on_start=False,
            temp_dir=tmp_path,
        )
        kh = _keywords_hash(settings.stream_keywords)
        cf = tmp_path / "discovered_stations.json"
        _save_cache(cf, stations, keywords_hash=kh)

        with (
            patch(
                "radio_ripper.services.playlist_discovery._is_cache_fresh",
                return_value=True,
            ),
            patch.object(PlaylistDiscoveryService, "_discover") as mock_discover,
        ):
            svc = PlaylistDiscoveryService(settings)
            result = await svc.load_or_discover()
        assert len(result) == 1
        assert result[0].name == "Rock FM"
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_discovery_when_cache_stale(self, tmp_path: Path) -> None:
        settings = Settings(
            destination="./rec",
            database="./rec/ripper.db",
            discovery_enabled=True,
            temp_dir=tmp_path,
        )
        cf = tmp_path / "discovered_stations.json"
        _save_cache(cf, [], keywords_hash=_keywords_hash(settings.stream_keywords))

        with (
            patch(
                "radio_ripper.services.playlist_discovery._is_cache_fresh",
                return_value=False,
            ),
            patch.object(PlaylistDiscoveryService, "_discover") as mock_discover,
        ):
            mock_discover.return_value = [
                StreamConfig(name="Rock FM", url="http://a", icy=True),
            ]
            svc = PlaylistDiscoveryService(settings)
            result = await svc.load_or_discover()
        assert len(result) == 1
        mock_discover.assert_called_once()


# ---------------------------------------------------------------------------
# git sparse checkout & parse_checked_dir
# ---------------------------------------------------------------------------


class TestParseCheckedDir:
    def test_nonexistent_dir_returns_empty(self) -> None:
        assert _parse_checked_dir(Path("/nonexistent")) == []

    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "+checked+"
        d.mkdir()
        assert _parse_checked_dir(d) == []

    def test_single_m3u(self, tmp_path: Path) -> None:
        d = tmp_path / "+checked+"
        d.mkdir()
        (d / "rock.m3u").write_text(
            "#EXTM3U\n#EXTINF:-1,Rock FM\nhttp://rock.example.com\n",
        )
        entries = _parse_checked_dir(d)
        assert len(entries) == 1
        assert entries[0].name == "Rock FM"
        assert entries[0].url == "http://rock.example.com"
        assert entries[0].source == "rock.m3u"

    def test_multiple_m3us(self, tmp_path: Path) -> None:
        d = tmp_path / "+checked+"
        d.mkdir()
        (d / "rock.m3u").write_text("#EXTM3U\n#EXTINF:-1,Rock FM\nhttp://a\n")
        (d / "pop.m3u").write_text("#EXTM3U\n#EXTINF:-1,Pop FM\nhttp://b\n")
        entries = _parse_checked_dir(d)
        assert len(entries) == 2
        assert {e.name for e in entries} == {"Rock FM", "Pop FM"}

    def test_ignores_non_m3u(self, tmp_path: Path) -> None:
        d = tmp_path / "+checked+"
        d.mkdir()
        (d / "readme.txt").write_text("hello")
        (d / "rock.m3u").write_text("#EXTM3U\n#EXTINF:-1,Rock FM\nhttp://a\n")
        assert len(_parse_checked_dir(d)) == 1

    def test_ignores_bad_encoding(self, tmp_path: Path) -> None:
        d = tmp_path / "+checked+"
        d.mkdir()
        (d / "bad.m3u").write_bytes(b"\xff\xfe")
        assert _parse_checked_dir(d) == []


class TestGitSparseCheckout:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        async def fake_proc(*args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.communicate = AsyncMock(return_value=(b"", b""))
            return m

        with (
            patch.object(Path, "rglob", return_value=[]),
            patch("asyncio.create_subprocess_exec", side_effect=fake_proc),
        ):
            tmp_dir, checked_dir = await _git_sparse_checkout()
            assert checked_dir.name == "+checked+"
            assert tmp_dir.is_dir()
            # clean up the real temp dir that was created
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_clone_failure(self) -> None:
        async def fail_proc(*args, **kwargs):
            m = MagicMock()
            m.returncode = 1
            m.communicate = AsyncMock(return_value=(b"", b"fatal: repository not found"))
            return m

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fail_proc),
            pytest.raises(RuntimeError, match="git clone failed"),
        ):
            await _git_sparse_checkout()
