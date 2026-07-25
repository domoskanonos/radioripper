from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

_logger = logging.getLogger("radio_ripper.resilience")


def retry_async(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[BaseException, int], None] | None = None,
    sleep_func: Callable[[float], Awaitable[None]] | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    sleep = sleep_func or asyncio.sleep

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = base_delay
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        raise
                    if on_retry:
                        on_retry(exc, attempt)
                    else:
                        _logger.debug(
                            "retry %s/%s for %s in %.1fs: %s",
                            attempt,
                            max_attempts - 1,
                            fn.__qualname__,
                            delay,
                            exc,
                        )
                    await sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


__all__ = ["retry_async"]
