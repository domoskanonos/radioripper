"""Tests for radio_ripper.services.playlist."""

from __future__ import annotations

from typing import Any

from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.playlist import (
    HttpPlaylistResolver,
    StaticPlaylistResolver,
    parse_m3u,
    parse_pls,
)


class TestParseM3u:
    def test_basic(self):
        text = "#EXTM3U\nhttp://stream1.example.com/mp3\nhttp://stream2.example.com/mp3\n"
        assert parse_m3u(text) == [
            "http://stream1.example.com/mp3",
            "http://stream2.example.com/mp3",
        ]

    def test_skips_comments_and_empty(self):
        text = "#EXTM3U\n#EXTINF:-1,Station\n\nhttp://x.com/s\n"
        assert parse_m3u(text) == ["http://x.com/s"]

    def test_only_urls_with_scheme(self):
        text = "not_a_url\nhttp://ok.com/s\n"
        assert parse_m3u(text) == ["http://ok.com/s"]

    def test_empty_input(self):
        assert parse_m3u("") == []


class TestParsePls:
    def test_basic(self):
        text = "[playlist]\nFile1=http://a.com/s\nTitle1=Station A\nFile2=http://b.com/s\n"
        assert parse_pls(text) == ["http://a.com/s", "http://b.com/s"]

    def test_skips_non_file_lines(self):
        text = "[playlist]\nNumberOfEntries=2\nFile1=http://a.com/s\n"
        assert parse_pls(text) == ["http://a.com/s"]

    def test_empty(self):
        assert parse_pls("") == []

    def test_skips_file_line_without_http(self):
        text = "[playlist]\nFile1=relative/path.mp3\nFile2=http://real.com/s\n"
        assert parse_pls(text) == ["http://real.com/s"]


class TestStaticPlaylistResolver:
    async def test_returns_urls(self):
        r = StaticPlaylistResolver(["http://a.com/s", "http://b.com/s"])
        urls = await r.resolve("doesnt-matter")
        assert urls == ["http://a.com/s", "http://b.com/s"]

    async def test_returns_copy(self):
        r = StaticPlaylistResolver(["http://a.com/s"])
        urls1 = await r.resolve("x")
        urls1.append("new")
        urls2 = await r.resolve("x")
        assert urls2 == ["http://a.com/s"]


class FakeClient:
    def __init__(self, text: str = "") -> None:
        self._text = text

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        return self._text

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        return {}

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        return b""

    async def aclose(self) -> None:
        pass


class TestHttpPlaylistResolver:
    async def test_resolves_m3u(self):
        client = HttpxAsyncClient()
        m3u_text = "#EXTM3U\nhttp://stream.example.com/audio\n"
        with __import__("respx").mock:
            __import__("respx").get("http://pls.example.com/listen.m3u").respond(text=m3u_text)
            resolver = HttpPlaylistResolver(client, timeout=5.0)
            urls = await resolver.resolve("http://pls.example.com/listen.m3u")
        assert urls == ["http://stream.example.com/audio"]
        await client.aclose()

    async def test_resolves_pls(self):
        client = HttpxAsyncClient()
        pls_text = "[playlist]\nFile1=http://stream.example.com/audio\n"
        with __import__("respx").mock:
            __import__("respx").get("http://pls.example.com/listen.pls").respond(text=pls_text)
            resolver = HttpPlaylistResolver(client, timeout=5.0)
            urls = await resolver.resolve("http://pls.example.com/listen.pls")
        assert urls == ["http://stream.example.com/audio"]
        await client.aclose()

    async def test_empty_playlist_returns_empty(self):
        client = HttpxAsyncClient()
        with __import__("respx").mock:
            __import__("respx").get("http://pls.example.com/empty.m3u").respond(text="#EXTM3U\n")
            resolver = HttpPlaylistResolver(client, timeout=5.0)
            urls = await resolver.resolve("http://pls.example.com/empty.m3u")
        assert urls == []
        await client.aclose()

    async def test_direct_stream_url_returns_as_is(self):
        client = FakeClient()
        resolver = HttpPlaylistResolver(client, timeout=5.0)
        urls = await resolver.resolve("http://example.com/stream")
        assert urls == ["http://example.com/stream"]

    async def test_m3u8_extension(self):
        client = FakeClient("#EXTM3U\nhttp://example.com/s\n")
        resolver = HttpPlaylistResolver(client, timeout=5.0)
        urls = await resolver.resolve("http://example.com/playlist.m3u8")
        assert urls == ["http://example.com/s"]

    async def test_pls_detected_via_content_sniff(self):
        client = FakeClient("File1=http://example.com/audio\n")
        resolver = HttpPlaylistResolver(client, timeout=5.0)
        url = "http://example.com/listen.m3u"
        urls = await resolver.resolve(url)
        assert urls == ["http://example.com/audio"]

    async def test_uppercase_m3u_still_treated_as_playlist(self):
        client = FakeClient("#EXTM3U\nhttp://parsed.example.com/audio\n")
        resolver = HttpPlaylistResolver(client, timeout=5.0)
        urls = await resolver.resolve("HTTP://EXAMPLE.COM/LISTEN.M3U")
        assert urls == ["http://parsed.example.com/audio"]

    async def test_unknown_extension_not_treated_as_playlist(self):
        client = FakeClient("should not be fetched")
        resolver = HttpPlaylistResolver(client, timeout=5.0)
        urls = await resolver.resolve("http://example.com/stream.aac")
        assert urls == ["http://example.com/stream.aac"]
