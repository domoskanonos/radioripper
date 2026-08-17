"""validation.py — Validierung aufgenommener Tracks (Größe + Dauer)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from radio_ripper.config import Settings

_LOGGER = logging.getLogger("radio_ripper.validation")


def _ffprobe_duration_sync(path: Path) -> float | None:
    """Führt ffprobe aus (blockierend) und gibt die Dauer in Sekunden zurück."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        _LOGGER.warning("ffprobe nicht gefunden — Dauer kann nicht ermittelt werden.")
        return None

    try:
        proc = subprocess.run(  # noqa: S603  -- Pfad kommt aus shutil.which, kein untrusted Input
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            timeout=10,
        )
        val = proc.stdout.decode().strip()
        if not val:
            return None
        return float(val)
    except Exception:
        return None


async def _get_duration(path: Path, executor: ThreadPoolExecutor) -> float | None:
    """Ermittelt die Datei-Dauer asynchron im ThreadPool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _ffprobe_duration_sync, path)


async def validate_file(path: Path, settings: Settings, executor: ThreadPoolExecutor) -> bool:
    """Gibt True nur, wenn BEIDE Validierungs-Tests bestanden sind.

    Test 1: Datei ist groß genug (min_file_size_bytes).
    Test 2: Track ist länger als min_file_duration_s.
    Schlägt ein Test fehl, wird die Datei gelöscht und False zurückgegeben.
    """
    # TEST 1: Mindest-Größe
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < settings.min_file_size_bytes:
        _LOGGER.info("Zu klein (%d < %d): %s", size, settings.min_file_size_bytes, path.name)
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return False

    # TEST 2: Mindest-Dauer
    if settings.min_file_duration_s > 0:
        dur = await _get_duration(path, executor)
        if dur is None:
            _LOGGER.warning("Dauer nicht bestimmbar — Datei wird behalten: %s", path.name)
        elif dur < settings.min_file_duration_s:
            _LOGGER.info(
                "Zu kurz (%.1fs < %.1fs): %s",
                dur,
                settings.min_file_duration_s,
                path.name,
            )
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            return False

    return True
