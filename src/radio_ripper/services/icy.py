"""Pure-Python ICY metadata parser (state machine).

This module contains zero I/O: it ingests bytes and emits metadata events.
The state machine is fully testable without network or threads.

States::

    WAIT_AUDIO → (consumed metaint bytes) → READ_META_LEN
    READ_META_LEN → (1 byte read) → READ_META
    READ_META → (consume meta_len bytes) → WAIT_AUDIO
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import Enum, auto

from radio_ripper.domain.models import TrackInfo

_STREAMTITLE_RE = re.compile(r"StreamTitle='(.*?)';", re.DOTALL)


class IcyEvent:
    """Marker base class for events emitted by :class:`IcyParser`.

    Subclasses: :class:`AudioChunk`, :class:`TitleChanged`.
    """


class AudioChunk(IcyEvent):
    """A continuous block of audio bytes belonging to the current song."""

    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __repr__(self) -> str:
        return f"AudioChunk(len={len(self.data)})"


class TitleChanged(IcyEvent):
    """A new StreamTitle was observed in the ICY metadata block."""

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
    """RFC-ish ICY metadata byte-stream parser.

    Feed it chunks via :meth:`feed`; iterate :meth:`events` to drain pending
    events. The parser keeps leftover bytes internally so callers can pass
    arbitrarily sized chunks.

    Args:
        metaint: Number of audio bytes between two metadata blocks.
            Must be a positive integer.
        max_meta_len: Safety cap on the metadata length byte (in bytes).
    """

    def __init__(self, metaint: int, *, max_meta_len: int = 16 * 255) -> None:
        if metaint <= 0:
            raise ValueError("metaint must be positive")
        self.metaint = metaint
        self.max_meta_len = max_meta_len
        self._state = _State.WAIT_AUDIO
        self._buffer = bytearray()
        self._bytes_until_meta = metaint
        self._meta_len_remaining = 0
        self._pending_events: list[IcyEvent] = []

    def feed(self, chunk: bytes) -> None:
        """Append ``chunk`` to the internal buffer."""
        self._buffer.extend(chunk)

    def events(self) -> Iterator[IcyEvent]:
        """Drain all events currently producible from the buffered bytes."""
        while self._pending_events:
            yield self._pending_events.pop(0)
        while True:
            produced = self._step()
            if not produced:
                break
            yield from self._step_drain()

    def _step_drain(self) -> Iterator[IcyEvent]:
        """Yield events deposited by :meth:`_step`."""
        while self._pending_events:
            yield self._pending_events.pop(0)

    def _step(self) -> bool:
        """Process one state transition consuming bytes from the buffer.

        Returns:
            ``True`` if a transition occurred (more bytes might still be
            consumable), ``False`` if more input is needed.
        """
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
                from radio_ripper.infra.errors import StreamProtocolError

                raise StreamProtocolError(
                    f"metadata length {meta_len} exceeds bound {self.max_meta_len}"
                )
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

        return False  # pragma: no cover

    @property
    def state(self) -> _State:
        """Current internal state (for diagnostics/testing)."""
        return self._state


def _parse_stream_title(meta_bytes: bytes) -> str | None:
    """Decode an ICY metadata block and extract ``StreamTitle``.

    Returns:
        The extracted title, or ``None`` if no ``StreamTitle`` field was found.
    """
    if not meta_bytes:
        return None
    text = meta_bytes.rstrip(b"\x00 ").decode("utf-8", errors="replace")
    m = _STREAMTITLE_RE.search(text)
    if m is None:
        return None
    title = m.group(1)
    title = title.replace("\\'", "'").replace("\\\\", "\\")
    return title.strip() or None


def split_track_info(stream_title: str) -> TrackInfo:
    """Convenience helper splitting a StreamTitle into a :class:`TrackInfo`."""
    return TrackInfo.from_stream_title(stream_title)


__all__ = [
    "AudioChunk",
    "IcyEvent",
    "IcyParser",
    "TitleChanged",
    "split_track_info",
]
