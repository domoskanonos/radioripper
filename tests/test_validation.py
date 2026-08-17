"""Tests für radio_ripper.validation — die 2 Kern-Validierungs-Checks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.config import Settings
from radio_ripper.validation import _get_duration, validate_file


@pytest.mark.asyncio
async def test_min_file_size_rejects_small(tmp_path: Path) -> None:
    """Test 1: Datei unter min_file_size_bytes wird verworfen und gelöscht."""
    settings = Settings(min_file_size_bytes=1_572_864)
    small = tmp_path / "small.mp3"
    small.write_bytes(b"x" * 100)  # 100 bytes < 1.5 MB

    with patch("radio_ripper.validation._get_duration", new=AsyncMock(return_value=120.0)):
        assert await validate_file(small, settings, ThreadPoolExecutor(1)) is False
    assert not small.exists(), "Zu kleine Datei wurde nicht gelöscht"


@pytest.mark.asyncio
async def test_min_file_size_accepts_big(tmp_path: Path) -> None:
    """Test 1b: Datei ab min_file_size_bytes wird behalten."""
    settings = Settings(min_file_size_bytes=1_572_864)
    big = tmp_path / "big.mp3"
    big.write_bytes(b"x" * (1_572_864 + 1))

    with patch("radio_ripper.validation._get_duration", new=AsyncMock(return_value=120.0)):
        ok = await validate_file(big, settings, ThreadPoolExecutor(1))
    assert ok, "Große Datei sollte den Größen-Test bestehen"
    assert big.exists()


@pytest.mark.asyncio
async def test_min_duration_rejects_short(tmp_path: Path) -> None:
    """Test 2: Track unter min_file_duration_s wird verworfen und gelöscht."""
    settings = Settings(min_file_duration_s=90.0)
    short = tmp_path / "short.mp3"
    short.write_bytes(b"x" * (1_572_864 + 1))

    with patch("radio_ripper.validation._get_duration", new=AsyncMock(return_value=60.0)):
        assert await validate_file(short, settings, ThreadPoolExecutor(1)) is False
    assert not short.exists(), "Zu kurze Datei wurde nicht gelöscht"


@pytest.mark.asyncio
async def test_min_duration_accepts_long(tmp_path: Path) -> None:
    """Test 2b: Track ab min_file_duration_s wird behalten."""
    settings = Settings(min_file_duration_s=90.0)
    long_mp3 = tmp_path / "long.mp3"
    long_mp3.write_bytes(b"x" * (1_572_864 + 1))

    with patch("radio_ripper.validation._get_duration", new=AsyncMock(return_value=120.0)):
        assert await validate_file(long_mp3, settings, ThreadPoolExecutor(1)) is True
    assert long_mp3.exists(), "Langer Track sollte behalten werden"


@pytest.mark.asyncio
async def test_get_duration_missing_ffprobe(tmp_path: Path) -> None:
    """Wenn ffprobe fehlt, wird None geliefert statt eines Fehlers."""
    from radio_ripper.validation import _ffprobe_duration_sync

    assert _ffprobe_duration_sync(tmp_path / "nonexistent.mp3") is None
