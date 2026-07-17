"""Tests for radio_ripper.services.playlist."""

from __future__ import annotations

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
