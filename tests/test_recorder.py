"""Tests für radio_ripper.recorder — StreamRecorder & cleanup_stale_parts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from radio_ripper.config import Settings
from radio_ripper.recorder import cleanup_stale_parts


def test_cleanup_stale_parts(tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir(parents=True)
    (recordings / "a.part").write_bytes(b"x")
    (recording := recordings / "b.part").write_bytes(b"y")
    (recordings / "keep.mp3").write_bytes(b"z")

    removed = cleanup_stale_parts(tmp_path)
    assert removed == 2
    assert not (recordings / "a.part").exists()
    assert not recording.exists()
    assert (recordings / "keep.mp3").exists()


def test_cleanup_stale_parts_no_dir(tmp_path: Path) -> None:
    assert cleanup_stale_parts(tmp_path) == 0


@pytest.mark.asyncio
async def test_recorder_make_writer(tmp_path: Path) -> None:
    from radio_ripper.models import StreamConfig
    from radio_ripper.recorder import StreamRecorder

    settings = Settings(work_dir=tmp_path)
    station = StreamConfig(name="Test", url="http://x.example/stream.mp3")
    rec = StreamRecorder(
        station=station,
        settings=settings,
        client=None,  # type: ignore[arg-type]
        executor=ThreadPoolExecutor(1),
    )
    writer = rec._make_writer("Test Song")
    assert writer is not None
    assert writer.final_path.parent == tmp_path / "recordings"
    assert writer.final_path.name == "Test Song.mp3"
    writer.discard()
