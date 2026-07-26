from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from enum import Enum, auto

from radio_ripper.infra.errors import StreamProtocolError

_STREAMTITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.DOTALL)


class IcyEvent: ...


class AudioChunk(IcyEvent):
    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __repr__(self) -> str:
        return f"AudioChunk(len={len(self.data)})"


class TitleChanged(IcyEvent):
    __slots__ = ("title",)

    def __init__(self, title: str) -> None:
        self.title = title

    def __repr__(self) -> str:
        return f"TitleChanged(title={self.title!r})"


class _State(Enum):
    WAIT_AUDIO = auto()
    READ_META_LEN = auto()
    READ_META = auto()


class IcyParser:
    def __init__(self, metaint: int, *, max_meta_len: int = 16 * 255) -> None:
        if metaint <= 0:
            raise StreamProtocolError(f"metaint must be positive, got {metaint}")
        self.metaint = metaint
        self.max_meta_len = max_meta_len
        self._state = _State.WAIT_AUDIO
        self._buffer = bytearray()
        self._bytes_until_meta = metaint
        self._meta_len_remaining = 0
        self._pending_events: deque[IcyEvent] = deque()

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def events(self) -> Iterator[IcyEvent]:
        while self._pending_events:
            yield self._pending_events.popleft()
        while True:
            produced = self._step()
            if not produced:
                break
            yield from self._step_drain()

    def _step_drain(self) -> Iterator[IcyEvent]:
        while self._pending_events:
            yield self._pending_events.popleft()

    def _step(self) -> bool:
        if not self._buffer:
            return False

        if self._state == _State.WAIT_AUDIO:
            if self._bytes_until_meta > 0:
                take = min(self._bytes_until_meta, len(self._buffer))
                if take <= 0:
                    return False
                data = bytes(self._buffer[:take])
                del self._buffer[:take]
                self._bytes_until_meta -= take
                self._pending_events.append(AudioChunk(data))
                return True
            self._state = _State.READ_META_LEN
            return True

        if self._state == _State.READ_META_LEN:
            if len(self._buffer) < 1:
                return False
            meta_len = self._buffer[0] * 16
            del self._buffer[:1]
            if meta_len > self.max_meta_len:
                raise StreamProtocolError(f"metadata length {meta_len} exceeds bound {self.max_meta_len}")
            self._meta_len_remaining = meta_len
            self._state = _State.READ_META
            return True

        if self._state == _State.READ_META:
            if len(self._buffer) < self._meta_len_remaining:
                return False
            meta_bytes = bytes(self._buffer[: self._meta_len_remaining])
            del self._buffer[: self._meta_len_remaining]
            self._meta_len_remaining = 0
            self._bytes_until_meta = self.metaint
            self._state = _State.WAIT_AUDIO
            title = _parse_stream_title(meta_bytes)
            if title is not None:
                self._pending_events.append(TitleChanged(title))
            return True

        return False

    @property
    def state(self) -> _State:
        return self._state


def _parse_stream_title(meta_bytes: bytes) -> str | None:
    if not meta_bytes:
        return None
    text = meta_bytes.rstrip(b"\x00 ").decode("utf-8", errors="replace")
    m = _STREAMTITLE_RE.search(text)
    if m is None:
        return None
    title = m.group(1).replace("\\'", "'").replace("\\\\", "\\").strip()
    return title if title else ""


__all__ = [
    "AudioChunk",
    "IcyEvent",
    "IcyParser",
    "TitleChanged",
]
