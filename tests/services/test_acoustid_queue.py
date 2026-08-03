"""Tests for radio_ripper.services.acoustid_queue.

The queue is ``work_dir/unchecked_mp3`` itself: a worker scans the directory,
processes the oldest file first, and retries failures with back-off.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.infra.config import Settings
from radio_ripper.services.acoustid_queue import AcoustidQueue


class _FakeHttp:
    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        return "{}"


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "work_dir": str(tmp_path / "work"),
        "destination": str(tmp_path / "work" / "destination"),
        "min_file_size_bytes": 1,
        "min_file_duration_s": 0,  # skip ffprobe duration check in tests
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _make_queue(tmp_path: Path, **kwargs) -> AcoustidQueue:
    settings = _make_settings(tmp_path)
    return AcoustidQueue(
        settings=settings,
        api_key="test-key",
        destination=settings.destination,
        http_client=_FakeHttp(),
        **kwargs,
    )


def _valid_mp3() -> bytes:
    # First MPEG frame header byte pair so is_valid_mp3 passes
    return b"\xff\xe0\x90\x00" + b"\x00" * 100


class TestDirectoryAsQueue:
    def test_enqueue_never_deletes_file(self, tmp_path):
        settings = _make_settings(tmp_path, max_unchecked_files=100)
        queue = AcoustidQueue(
            settings=settings,
            api_key="k",
            destination=settings.destination,
            http_client=_FakeHttp(),
        )
        # Create 101 files so the staging dir is over its configured limit
        staging = settings.work_dir / "unchecked_mp3"
        staging.mkdir(parents=True)
        for i in range(101):
            f = staging / f"f{i}.mp3"
            f.write_bytes(b"x")
        overflow = staging / "overflow.mp3"
        overflow.write_bytes(b"x")
        queue.enqueue(overflow)
        # The file must never be deleted, only reported over-limit
        assert overflow.exists()

    def test_pick_next_returns_oldest_first(self, tmp_path):
        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        newest = staging / "newest.mp3"
        oldest = staging / "oldest.mp3"
        newest.write_bytes(b"b")
        oldest.write_bytes(b"a")
        os.utime(oldest, (time.time() - 1000, time.time() - 1000))
        os.utime(newest, (time.time(), time.time()))
        assert queue._pick_next() == oldest

    def test_pick_next_skips_files_in_cooldown(self, tmp_path):
        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        a = staging / "a.mp3"
        b = staging / "b.mp3"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        # Put a in cooldown (retry in the future), b has no cooldown
        queue._retry_info[a] = (time.monotonic() + 3600.0, 1)
        assert queue._pick_next() == b

    def test_load_existing_unchecked_cleans_part_files(self, tmp_path):
        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        (staging / "pending.mp3").write_bytes(b"x")
        (staging / "crashed.part").write_bytes(b"x")
        count = queue.load_existing_unchecked()
        assert count == 1
        assert not (staging / "crashed.part").exists()
        assert (staging / "pending.mp3").exists()

    @pytest.mark.asyncio
    async def test_worker_deletes_rejected_file(self, tmp_path):
        from radio_ripper.services.storage import AcoustidLookup

        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        f = staging / "song.mp3"
        f.write_bytes(_valid_mp3())

        async def fake_lookup(path, api_key, **kwargs):
            return AcoustidLookup(outcome="rejected", reject_reason="below threshold")

        with patch("radio_ripper.services.acoustid_queue.acoustid_lookup", side_effect=fake_lookup):
            queue.start()
            try:
                await asyncio.sleep(0.3)
            finally:
                await queue.stop()
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_worker_retries_transient_error(self, tmp_path):
        from radio_ripper.services.storage import AcoustidLookup

        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        f = staging / "song.mp3"
        f.write_bytes(_valid_mp3())

        async def fake_lookup(path, api_key, **kwargs):
            return AcoustidLookup(outcome="error", error_detail="timeout")

        with patch("radio_ripper.services.acoustid_queue.acoustid_lookup", side_effect=fake_lookup):
            queue.start()
            try:
                await asyncio.sleep(0.3)
            finally:
                await queue.stop()
        # Transient error -> file must be kept for a later retry
        assert f.exists()
        assert f in queue._retry_info
