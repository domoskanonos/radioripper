"""Tests für radio_ripper.http_client — HttpxClient & resolve_playlist."""

from __future__ import annotations

import httpx
import pytest
import respx

from radio_ripper.http_client import HttpxClient, resolve_playlist


class _FakeClient:
    def __init__(self, text: str) -> None:
        self._text = text

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        return self._text


@pytest.mark.asyncio
async def test_direct_url_passthrough() -> None:
    client = _FakeClient("")
    result = await resolve_playlist(client, "http://x.example/stream.mp3")
    assert result == ["http://x.example/stream.mp3"]


@pytest.mark.asyncio
async def test_m3u_playlist_parsed() -> None:
    text = "#EXTM3U\n#EXTINF:-1,A\nhttp://a.example\n#EXTINF:-1,B\nhttp://b.example\n"
    client = _FakeClient(text)
    result = await resolve_playlist(client, "http://x.example/list.m3u")
    assert result == ["http://a.example", "http://b.example"]


@pytest.mark.asyncio
async def test_m3u8_playlist_parsed() -> None:
    text = "#EXTM3U\n#EXTINF:-1,A\nhttp://a.example/seg.ts\n"
    client = _FakeClient(text)
    result = await resolve_playlist(client, "http://x.example/list.m3u8")
    assert result == ["http://a.example/seg.ts"]


@pytest.mark.asyncio
async def test_httpxclient_get_text() -> None:
    with respx.mock:
        respx.get("https://test.example/text").mock(return_value=httpx.Response(200, text="hello"))
        async with HttpxClient() as client:
            assert await client.get_text("https://test.example/text") == "hello"


@pytest.mark.asyncio
async def test_httpxclient_stream_and_headers() -> None:
    with respx.mock:
        route = respx.get("https://test.example/stream").mock(
            return_value=httpx.Response(200, content=b"abc", headers={"icy-metaint": "8192"})
        )
        async with HttpxClient() as client:
            chunks = []
            async for chunk in client.stream_binary("https://test.example/stream"):
                chunks.append(chunk)
            assert b"".join(chunks) == b"abc"
            assert client.response_headers()["icy-metaint"] == "8192"
            assert route.called


@pytest.mark.asyncio
async def test_httpxclient_aclose() -> None:
    client = HttpxClient()
    await client.aclose()
    # aclose ist idempotent — zweiter Aufruf darf nicht crashen
    await client.aclose()


@pytest.mark.asyncio
async def test_httpxclient_get_text_http_error() -> None:
    with respx.mock:
        respx.get("https://test.example/404").mock(return_value=httpx.Response(404))
        async with HttpxClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_text("https://test.example/404")
