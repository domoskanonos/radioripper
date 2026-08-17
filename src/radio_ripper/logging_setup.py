"""logging_setup.py — Logging-Konfiguration für radio-ripper."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(level: str, log_file: Path | None = None) -> None:
    """Konfiguriert Konsolen- und optionale Datei-Logausgabe."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
