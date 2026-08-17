"""Tests für radio_ripper.http_client — resolve_playlist."""

from __future__ import annotations

import pytest

from radio_ripper.http_client import resolve_playlist


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
