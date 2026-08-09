"""Tests for radio_ripper.services.acoustid_queue.

The queue is ``work_dir/unchecked_mp3`` itself: a worker scans the directory,
processes the oldest file first, and retries failures with back-off.
"""

from __future__ import annotations

import asyncio
import errno
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.infra.config import Settings
from radio_ripper.services.acoustid_queue import AcoustidQueue, cleanup_stale_parts


def test_cleanup_stale_parts_removes_only_part(tmp_path):
    work = tmp_path / "work"
    staging = work / "unchecked_mp3"
    staging.mkdir(parents=True)
    (staging / "crashed.part").write_bytes(b"x")
    (staging / "finished.mp3").write_bytes(b"x")
    removed = cleanup_stale_parts(work)
    assert removed == 1
    assert not (staging / "crashed.part").exists()
    assert (staging / "finished.mp3").exists()


def test_cleanup_stale_parts_missing_dir_returns_zero(tmp_path):
    assert cleanup_stale_parts(tmp_path / "nope") == 0


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
    def test_rate_limiter_reads_live_rpm(self, tmp_path):
        """Hot-reloading acoustid_requests_per_minute changes the interval live."""
        settings = _make_settings(tmp_path, acoustid_requests_per_minute=60)
        queue = AcoustidQueue(
            settings=settings,
            api_key="k",
            destination=settings.destination,
            http_client=_FakeHttp(),
        )
        assert queue._rate_limiter._interval == pytest.approx(1.0)  # 60 rpm -> 1 s

        new_settings = _make_settings(tmp_path, acoustid_requests_per_minute=120)
        queue.update_settings(new_settings)
        assert queue._rate_limiter._interval == pytest.approx(0.5)  # 120 rpm -> 0.5 s

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

    def test_enqueue_does_not_full_scan(self, tmp_path):
        """enqueue() must not rescan the whole directory (O(n) hot path)."""
        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        for i in range(200):
            (staging / f"f{i}.mp3").write_bytes(b"x" * 100)
        queue._init_usage()  # initial scan only at startup

        new_file = staging / "new.mp3"
        new_file.write_bytes(b"y" * 10)

        glob_calls = 0
        original_glob = Path.glob

        def patched_glob(self, pattern):
            nonlocal glob_calls
            glob_calls += 1
            return original_glob(self, pattern)

        with patch.object(Path, "glob", patched_glob):
            queue.enqueue(new_file)

        assert glob_calls == 0
        # The new file is now reflected in the cached usage
        assert queue.usage() == (201, 200 * 100 + 10)

    def test_usage_cache_tracks_enqueue_and_removal(self, tmp_path):
        """The usage cache stays in sync with enqueue and delete/move."""
        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        a = staging / "a.mp3"
        b = staging / "b.mp3"
        a.write_bytes(b"x" * 10)
        b.write_bytes(b"x" * 20)

        queue._init_usage()
        assert queue.usage() == (2, 30)

        # Re-enqueue of an already-tracked file must not double-count
        queue.enqueue(a)
        assert queue.usage() == (2, 30)

        # A fresh enqueue is added to the cache without a scan
        c = staging / "c.mp3"
        c.write_bytes(b"x" * 5)
        queue.enqueue(c)
        assert queue.usage() == (3, 35)

        # Removals (rejected/committed/give-up) are decremented
        queue._remove_tracked(a)
        queue._remove_tracked(b)
        queue._remove_tracked(c)
        assert queue.usage() == (0, 0)

    def test_check_unchecked_limits_uses_cache(self, tmp_path):
        """Over-limit detection uses the incremental cache, not a directory scan."""
        settings = _make_settings(tmp_path, max_unchecked_files=100)
        queue = AcoustidQueue(
            settings=settings,
            api_key="k",
            destination=settings.destination,
            http_client=_FakeHttp(),
        )
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        for i in range(100):
            (staging / f"f{i}.mp3").write_bytes(b"x")
        queue._init_usage()

        overflow = staging / "overflow.mp3"
        overflow.write_bytes(b"x")

        glob_calls = 0
        original_glob = Path.glob

        def patched_glob(self, pattern):
            nonlocal glob_calls
            glob_calls += 1
            return original_glob(self, pattern)

        with patch.object(Path, "glob", patched_glob):
            assert queue._check_unchecked_limits() is False
        assert glob_calls == 0

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

    def test_schedule_retry_give_up_deletes_file(self, tmp_path):
        """Once the retry budget is exhausted the file is removed for good."""
        settings = _make_settings(tmp_path, acoustid_retry_max_attempts=2)
        queue = AcoustidQueue(
            settings=settings,
            api_key="k",
            destination=settings.destination,
            http_client=_FakeHttp(),
        )
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        f = staging / "broken.mp3"
        f.write_bytes(b"x")
        queue._init_usage()
        assert queue.usage() == (1, 1)

        # max_attempts=2 -> max_attempts window is 3, give-up on the 4th call
        queue._schedule_retry(f, 1.0)
        queue._schedule_retry(f, 1.0)
        assert f.exists()
        queue._schedule_retry(f, 1.0)
        queue._schedule_retry(f, 1.0)
        assert not f.exists()
        assert f not in queue._retry_info
        assert queue.usage() == (0, 0)

    @pytest.mark.asyncio
    async def test_worker_deletes_file_after_max_retries(self, tmp_path):
        """Persistent transient errors must end with the file being deleted."""
        from radio_ripper.services.storage import AcoustidLookup

        settings = _make_settings(
            tmp_path,
            acoustid_retry_max_attempts=0,
            acoustid_retry_base_delay=1.0,
            acoustid_requests_per_minute=180,
        )
        queue = AcoustidQueue(
            settings=settings,
            api_key="k",
            destination=settings.destination,
            http_client=_FakeHttp(),
        )
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        f = staging / "song.mp3"
        f.write_bytes(_valid_mp3())

        async def fake_lookup(path, api_key, **kwargs):
            return AcoustidLookup(outcome="error", error_detail="timeout")

        with patch("radio_ripper.services.acoustid_queue.acoustid_lookup", side_effect=fake_lookup):
            queue.start()
            try:
                await asyncio.sleep(2.5)
            finally:
                await queue.stop()
        assert not f.exists()
        assert f not in queue._retry_info


class TestCommitCrossDevice:
    @pytest.mark.asyncio
    async def test_commit_falls_back_across_devices(self, tmp_path):
        from radio_ripper.services.storage import AcoustidLookup, AcoustidMatch, is_valid_mp3

        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        src = staging / "song.mp3"
        src.write_bytes(_valid_mp3())
        result = AcoustidLookup(outcome="accepted", match=AcoustidMatch(artist="Artist", title="Title", score=0.95))
        with patch("radio_ripper.services.storage.os.replace", side_effect=OSError(errno.EXDEV, "cross-device link")):
            committed = await queue._commit(src, result)

        assert committed is True
        assert not src.exists()
        target = queue._destination / "Artist - Title.mp3"
        assert target.exists()
        # Tags were written before the move and the content arrived intact
        assert target.read_bytes()[:3] == b"ID3"
        assert await is_valid_mp3(target) is True

    @pytest.mark.asyncio
    async def test_commit_keeps_source_on_real_move_error(self, tmp_path):
        from radio_ripper.services.storage import AcoustidLookup, AcoustidMatch

        queue = _make_queue(tmp_path)
        staging = queue.unchecked_dir
        staging.mkdir(parents=True)
        src = staging / "song.mp3"
        src.write_bytes(_valid_mp3())
        result = AcoustidLookup(outcome="accepted", match=AcoustidMatch(artist="Artist", title="Title", score=0.95))
        with patch("radio_ripper.services.storage.os.replace", side_effect=OSError(errno.EACCES, "permission denied")):
            committed = await queue._commit(src, result)

        # Non-EXDEV failures are real errors -> retry, source is retained
        assert committed is False
        assert src.exists()
        assert not (queue._destination / "Artist - Title.mp3").exists()
