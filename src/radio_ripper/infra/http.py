from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

import httpx


class AsyncHttpClient(ABC):
    @abstractmethod
    async def get_text(self, url: str, *, timeout: float | None = None) -> str: ...

    @abstractmethod
    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any: ...

    @abstractmethod
    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes: ...
    @abstractmethod
    def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[bytes, None]: ...

    @abstractmethod
    def response_headers(self) -> dict[str, str]: ...

    @abstractmethod
    async def aclose(self) -> None: ...


class HttpxAsyncClient(AsyncHttpClient):
    def __init__(
        self,
        *,
        user_agent: str = "Radio-Ripper/2.0",
        verify: bool = True,
        connect_timeout: float = 10.0,
        total_timeout: float = 30.0,
        max_pool_size: int = 400,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            verify=verify,
            follow_redirects=True,
            timeout=httpx.Timeout(total_timeout, connect=connect_timeout),
            limits=httpx.Limits(
                max_connections=max_pool_size,
                max_keepalive_connections=max_pool_size,
            ),
        )
        self._last_headers: dict[str, str] = {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        resp = await self._client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    async def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncGenerator[bytes, None]:
        async with self._client.stream(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            self._last_headers = dict(resp.headers)
            async for chunk in resp.aiter_bytes():
                yield chunk

    def response_headers(self) -> dict[str, str]:
        return dict(self._last_headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpxAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["AsyncHttpClient", "HttpxAsyncClient"]
