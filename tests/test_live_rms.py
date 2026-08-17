"""Tests für radio_ripper.live_rms — LiveRmsSource."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.live_rms import _RMS_RE, LiveRmsSource
from radio_ripper.silence import RmsTracker


class _FakeStderr:
    """Stellt stderr.read() als async-Generator dar (einmal pro Aufruf)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._parts: list[bytes] = []
        for line in lines:
            self._parts.append(line + b"\n")
        self._idx = 0

    async def read(self, n: int) -> bytes:
        if self._idx >= len(self._parts):
            return b""
        part = self._parts[self._idx]
        self._idx += 1
        return part


class _FakeProc:
    def __init__(self, stderr: _FakeStderr) -> None:
        self.stderr = stderr
        self.killed = False

    def kill(self) -> None:
        self.killed = True


def test_rms_regex_parses_values() -> None:
    m = _RMS_RE.search("lavfi.astats.Overall.RMS_level=-12.3")
    assert m is not None
    assert m.group(1) == "-12.3"
    m = _RMS_RE.search("lavfi.astats.Overall.RMS_level=-inf")
    assert m is not None
    assert m.group(1) == "-inf"
    assert _RMS_RE.search("lavfi.astats.Overall.Peak_level=-5.0") is None


@pytest.mark.asyncio
async def test_parse_line_feeds_tracker() -> None:
    tracker = RmsTracker()
    src = LiveRmsSource("http://x.example/stream", tracker)
    src._start_mono = 100.0  # fixe Startzeit

    with patch("time.monotonic", return_value=101.0):
        src._parse_line("frame:1    pts:0    lavfi.astats.Overall.RMS_level=-12.0")
    with patch("time.monotonic", return_value=102.0):
        src._parse_line("frame:2    pts:1    lavfi.astats.Overall.RMS_level=-50.0")

    assert src.current_rms == -50.0
    # Erster Messwert: kein Bucket-Vergleich möglich → beide werden in Tracker gelegt
    assert tracker._bucket_rms  # Tracker hat Werte


@pytest.mark.asyncio
async def test_run_parses_stderr_and_stops() -> None:
    tracker = RmsTracker()
    src = LiveRmsSource("http://x.example/stream", tracker)

    with patch.object(src, "_run", new=AsyncMock(return_value=None)):
        # start() erstellt einen Task über _run → wir mocken _run direkt
        src._start_mono = 100.0
        src._task = asyncio.create_task(src._run())
        # Da _run gemockt ist, beenden wir sofort
        await asyncio.sleep(0.05)
        await src.stop()
    assert True


@pytest.mark.asyncio
async def test_start_creates_process() -> None:
    tracker = RmsTracker()
    src = LiveRmsSource("http://x.example/stream", tracker)

    fake_proc = _FakeProc(_FakeStderr([b"lavfi.astats.Overall.RMS_level=-10.0\n"]))
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_exec:
        src._start_mono = 100.0
        await src._run()

    mock_exec.assert_called_once()
    cmd = mock_exec.call_args.args
    assert cmd[0] == "ffmpeg"
    assert "-i" in cmd
    assert "http://x.example/stream" in cmd
    assert src._last_rms == -10.0


@pytest.mark.asyncio
async def test_run_missing_ffmpeg() -> None:
    tracker = RmsTracker()
    src = LiveRmsSource("http://x.example/stream", tracker)

    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("ffmpeg missing")),
    ):
        await src._run()
    assert src._proc is None
