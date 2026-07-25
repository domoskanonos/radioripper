"""Pytest fixtures for stream tests."""

from __future__ import annotations

from pathlib import Path

import pytest




@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_ripper.db"


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    d = tmp_path / "recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mp3_bytes() -> bytes:
    return b"\xff\xfb" + b"\x00" * 4094
