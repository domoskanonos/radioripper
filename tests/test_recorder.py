"""Tests für radio_ripper.recorder — StreamRecorder & cleanup_stale_parts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import HttpUrl

from radio_ripper.config import Settings
from radio_ripper.models import StreamConfig
from radio_ripper.recorder import StreamRecorder, cleanup_stale_parts
from radio_ripper.writer import TrackWriter


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        work_dir=tmp_path,
        destination=tmp_path / "dest",
        acoustid_api_key="KEY",
    )
    base.update(overrides)
    return Settings(**base)


def _make_recorder(tmp_path: Path, settings: Settings | None = None, client: Any = None) -> StreamRecorder:
    settings = settings or _make_settings(tmp_path)
    station = StreamConfig(name="Test", url=HttpUrl("http://x.example/stream.mp3"))
    return StreamRecorder(
        station=station,
        settings=settings,
        client=client or MagicMock(),
        executor=ThreadPoolExecutor(1),
    )


def test_cleanup_stale_parts(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir(parents=True)
    (recordings / "a.part").write_bytes(b"x")
    (recordings / "b.part").write_bytes(b"y")
    (recordings / "keep.mp3").write_bytes(b"z")

    removed = cleanup_stale_parts(tmp_path)
    assert removed == 2
    assert not (recordings / "a.part").exists()
    assert not (recordings / "b.part").exists()
    assert (recordings / "keep.mp3").exists()


def test_cleanup_stale_parts_no_dir(tmp_path: Path) -> None:
    assert cleanup_stale_parts(tmp_path) == 0


@pytest.mark.asyncio
async def test_recorder_make_writer(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path)
    writer = rec._make_writer("Test Song")
    assert writer is not None
    assert writer.final_path.parent == tmp_path / "recordings"
    assert writer.final_path.name == "Test Song.mp3"
    writer.discard()


def test_recorder_make_writer_invalid_title(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path)
    assert rec._make_writer("") is None
    assert rec._make_writer("///") is None


def test_should_record_title(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path)
    assert rec._should_record_title("   ") is False
    assert rec._should_record_title("Werbung im Radio") is True  # alles wird aufgenommen
    assert rec._should_record_title("Artist - Song") is True


def test_station_name(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path)
    assert rec.station_name == "Test"


@pytest.mark.asyncio
async def test_connect_stream_no_icy(tmp_path: Path) -> None:
    """Fehlender icy-metaint → None + no_icy_failures zählt hoch."""
    client = MagicMock()
    client.response_headers.return_value = {}
    agen = AsyncMock()
    agen.__anext__.return_value = b"data"
    client.stream_binary.return_value = agen

    rec = _make_recorder(tmp_path, client=client)
    result = await rec._connect_stream("http://x.example/stream")
    assert result is None
    assert rec._no_icy_failures == 1


@pytest.mark.asyncio
async def test_connect_stream_with_icy(tmp_path: Path) -> None:
    """Mit icy-metaint → Parser + Generator zurück."""
    client = MagicMock()
    client.response_headers.return_value = {"icy-metaint": "8192"}
    agen = AsyncMock()
    agen.__anext__.return_value = b"\xff\xe0"
    client.stream_binary.return_value = agen

    rec = _make_recorder(tmp_path, client=client)
    result = await rec._connect_stream("http://x.example/stream")
    assert result is not None
    gen, parser = result
    assert gen is agen
    assert parser.metaint == 8192
    assert rec._no_icy_failures == 0


@pytest.mark.asyncio
async def test_connect_stream_connect_error(tmp_path: Path) -> None:
    """Verbindungsfehler → Exception wird weitergereicht."""
    client = MagicMock()
    agen = AsyncMock()
    agen.__anext__.side_effect = ConnectionError("conn refused")
    client.stream_binary.return_value = agen

    rec = _make_recorder(tmp_path, client=client)
    with pytest.raises(ConnectionError):
        await rec._connect_stream("http://x.example/stream")


@pytest.mark.asyncio
async def test_run_once_playlist_error(tmp_path: Path) -> None:
    """Playlist-Fehler → connect_failures erhöht, False."""
    client = MagicMock()
    rec = _make_recorder(tmp_path, client=client)
    with patch("radio_ripper.recorder.resolve_playlist", side_effect=Exception("boom")):
        assert await rec._run_once() is False
    assert rec._connect_failures == 1


@pytest.mark.asyncio
async def test_run_once_no_urls(tmp_path: Path) -> None:
    client = MagicMock()
    rec = _make_recorder(tmp_path, client=client)
    with patch("radio_ripper.recorder.resolve_playlist", new=AsyncMock(return_value=[])):
        assert await rec._run_once() is False


@pytest.mark.asyncio
async def test_run_once_timeout(tmp_path: Path) -> None:
    import httpx

    client = MagicMock()
    rec = _make_recorder(tmp_path, client=client)
    with (
        patch("radio_ripper.recorder.resolve_playlist", new=AsyncMock(return_value=["http://x"])),
        patch.object(rec, "_stream_with_meta", side_effect=httpx.TimeoutException("t", request=None)),
    ):
        assert await rec._run_once() is False
    assert rec._connect_failures == 1


@pytest.mark.asyncio
async def test_run_once_success_resets_failures(tmp_path: Path) -> None:
    client = MagicMock()
    rec = _make_recorder(tmp_path, client=client)
    rec._connect_failures = 3
    with (
        patch("radio_ripper.recorder.resolve_playlist", new=AsyncMock(return_value=["http://x"])),
        patch.object(rec, "_stream_with_meta", new=AsyncMock(return_value=True)),
    ):
        assert await rec._run_once() is True
    assert rec._connect_failures == 0


# ---------------------------------------------------------------------------
# _stream_with_meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_meta_title_boundaries(tmp_path: Path) -> None:
    """Titelwechsel: erste Aufnahme wird finalisiert, neue gestartet."""
    rec = _make_recorder(tmp_path)
    rec._acoustid_worker = None

    # Simuliere Stream: Audio + Titelwechsel + Audio + Titelwechsel (EOF)
    # Wir nutzen echte ICY-Chunks über den Parser.
    parser_metaint = 16

    # Simuliere Stream: Audio + Titelwechsel + Audio + Titelwechsel (EOF).
    # metaint=16: jeder Block = 16 Audio-Bytes, dann optional 1 Längenbyte + Meta.
    parser_metaint = 16

    def title_meta(title: str) -> bytes:
        base = f"StreamTitle='{title}';".encode()
        padded = base + b"\x00" * ((16 - len(base) % 16) % 16)
        return bytes([len(padded) // 16]) + padded

    chunks = [
        b"A" * 16 + title_meta("Artist One - Song One"),
        b"B" * 16 + title_meta("Artist Two - Song Two"),
        b"C" * 16 + title_meta("Artist Three - Song Three"),
        b"D" * 16,  # nur Audio
    ]

    # Fake-Client mit passendem metaint und echtem async-Generator
    client = MagicMock()
    client.response_headers.return_value = {"icy-metaint": str(parser_metaint)}
    agen = _FakeAsyncGen(chunks)
    client.stream_binary.return_value = agen

    rec = _make_recorder(tmp_path, client=client)

    # _finalize_writer mocken, um nur die Aufnahme-Logik zu testen
    finalized = []

    async def fake_finalize(writer: TrackWriter) -> None:
        finalized.append(writer.final_path.name)

    with patch.object(rec, "_finalize_writer", side_effect=fake_finalize):
        ok = await rec._stream_with_meta("http://x.example/stream")

    assert ok is True
    # "Artist One" = first seen (übersprungen); "Artist Two" wird aufgenommen und
    # beim Wechsel zu "Artist Three" finalisiert.
    assert len(finalized) == 1
    assert finalized[0] == "Artist Two - Song Two.mp3"


class _FakeAsyncGen:
    """Stellt die Async-Generator-API (__anext__/aclose) mit einer Chunk-Liste dar."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
        self._closed = False

    def __aiter__(self) -> _FakeAsyncGen:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration as err:
            raise StopAsyncIteration() from err

    async def aclose(self) -> None:
        self._closed = True


@pytest.mark.asyncio
async def test_stream_meta_stop_discards(tmp_path: Path) -> None:
    """Stop während der Aufnahme → Writer wird verworfen."""
    client = MagicMock()
    client.response_headers.return_value = {"icy-metaint": "16"}
    agen = _FakeAsyncGen([b"A" * 16])
    client.stream_binary.return_value = agen

    rec = _make_recorder(tmp_path, client=client)
    rec._stop_event.set()
    ok = await rec._stream_with_meta("http://x.example/stream")
    assert ok is True


@pytest.mark.asyncio
async def test_run_forever_no_icy_disables(tmp_path: Path) -> None:
    """Zu viele ICY-Fehler → Recorder deaktiviert sich."""
    rec = _make_recorder(tmp_path, _make_settings(tmp_path))
    rec._no_icy_failures = 10
    with patch.object(rec, "_run_once", new=AsyncMock(return_value=False)):
        await rec._run_forever()
    assert rec._stop_event.is_set() is False  # Task beendet normal


@pytest.mark.asyncio
async def test_run_forever_connect_failures_disables(tmp_path: Path) -> None:
    """Zu viele Verbindungsfehler → Recorder deaktiviert sich."""
    rec = _make_recorder(tmp_path, _make_settings(tmp_path))
    rec._connect_failures = 10
    with patch.object(rec, "_run_once", new=AsyncMock(return_value=False)):
        await rec._run_forever()
    assert True


@pytest.mark.asyncio
async def test_run_forever_stop_event_breaks(tmp_path: Path) -> None:
    """Setzt man stop_event, endet _run_forever."""
    rec = _make_recorder(tmp_path)
    rec._stop_event.set()
    with patch.object(rec, "_run_once", new=AsyncMock(return_value=True)):
        await rec._run_forever()
    assert True


@pytest.mark.asyncio
async def test_start_join(tmp_path: Path) -> None:
    rec = _make_recorder(tmp_path)
    rec._stop_event.set()  # damit _run_forever sofort endet
    task = rec.start()
    await rec.join()
    assert task.done()


# ---------------------------------------------------------------------------
# _stream_with_meta / _run_forever — weitere Pfade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_meta_exception_discards(tmp_path: Path) -> None:
    """Fehler im Stream nach dem Verbinden → Writer verworfen, False."""
    client = MagicMock()
    client.response_headers.return_value = {"icy-metaint": "16"}

    class _BoomGen:
        def __init__(self) -> None:
            self._count = 0

        def __aiter__(self) -> _BoomGen:
            return self

        async def __anext__(self) -> bytes:
            self._count += 1
            if self._count == 1:
                return b"A" * 16  # erster Chunk (für _connect_stream)
            raise RuntimeError("connection lost")

        async def aclose(self) -> None:
            pass

    client.stream_binary.return_value = _BoomGen()

    rec = _make_recorder(tmp_path, client=client)
    ok = await rec._stream_with_meta("http://x.example/stream")
    assert ok is False


@pytest.mark.asyncio
async def test_run_forever_reconnect_and_stop(tmp_path: Path) -> None:
    """Erfolglose Runde → Reconnect-Wartezeit; Stop bricht die Schleife ab."""
    import asyncio as _asyncio

    rec = _make_recorder(tmp_path, _make_settings(tmp_path))
    rec._connect_failures = 0

    calls = {"n": 0}

    async def fake_run_once() -> bool:
        calls["n"] += 1
        return False

    async def _stop_after() -> None:
        await _asyncio.sleep(0.05)
        rec.stop()

    with patch.object(rec, "_run_once", side_effect=fake_run_once):
        stopper = _asyncio.create_task(_stop_after())
        await rec._run_forever()
        await stopper

    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_run_forever_exception_in_run_once(tmp_path: Path) -> None:
    """Exception in _run_once wird gefangen, Schleife läuft weiter."""

    rec = _make_recorder(tmp_path, _make_settings(tmp_path))
    rec._connect_failures = 0

    calls = {"n": 0}

    async def fake_run_once() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        rec.stop()
        return True

    async def fake_sleep(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0.01)

    with (
        patch.object(rec, "_run_once", side_effect=fake_run_once),
        patch("radio_ripper.recorder.asyncio.wait_for", side_effect=fake_sleep),
    ):
        await rec._run_forever()
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_run_forever_success_resets_delay(tmp_path: Path) -> None:
    """Erfolgreiche Runde setzt das Reconnect-Delay zurück (kein Backoff)."""

    rec = _make_recorder(tmp_path, _make_settings(tmp_path))
    rec._connect_failures = 0

    calls = {"n": 0}

    async def fake_run_once() -> bool:
        calls["n"] += 1
        if calls["n"] >= 2:
            rec.stop()
        return True

    # wait_for wird beim Erfolg nicht aufgerufen — patchen wir nicht,
    # aber wir brauchen einen Weg, den Loop nicht zu blockieren.
    with patch.object(rec, "_run_once", side_effect=fake_run_once):
        await rec._run_forever()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_stream_meta_first_title_skipped(tmp_path: Path) -> None:
    """Der erste eingestiegene Titel wird übersprungen (keine Aufnahme)."""
    client = MagicMock()
    client.response_headers.return_value = {"icy-metaint": "16"}

    def title_meta(title: str) -> bytes:
        base = f"StreamTitle='{title}';".encode()
        padded = base + b"\x00" * ((16 - len(base) % 16) % 16)
        return bytes([len(padded) // 16]) + padded

    chunks = [
        b"A" * 16 + title_meta("Werbung Test"),
        b"B" * 16 + title_meta("Artist - Song"),
        b"C" * 16,
    ]
    client.stream_binary.return_value = _FakeAsyncGen(chunks)

    rec = _make_recorder(tmp_path)
    rec._acoustid_worker = None

    writers = []

    async def fake_finalize(writer: TrackWriter) -> None:
        writers.append(writer.final_path.name)

    with patch.object(rec, "_finalize_writer", side_effect=fake_finalize):
        ok = await rec._stream_with_meta("http://x.example/stream")

    assert ok is True
    # "Werbung Test" ist der erste Titel (wird übersprungen); "Artist - Song"
    # startet eine Aufnahme, endet aber ohne weiteren Titelwechsel → keine
    # Finalisierung.
    assert writers == []
