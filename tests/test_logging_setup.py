"""Tests für radio_ripper.logging_setup — Logging-Konfiguration."""

from __future__ import annotations

import logging
from pathlib import Path

from radio_ripper.logging_setup import configure_logging


def test_configure_logging_stream_only() -> None:
    root = logging.getLogger()
    # Cleanup bestehender Handler für sauberen Test
    old_handlers = list(root.handlers)
    root.handlers.clear()
    try:
        configure_logging("INFO")
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)


def test_configure_logging_with_file(tmp_path: Path) -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    log_file = tmp_path / "test.log"
    try:
        configure_logging("DEBUG", log_file)
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 2
        assert log_file.parent.exists()
        # FileHandler schreibt beim Erstellen eine leere Datei
        assert log_file.exists()
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)


def test_configure_logging_invalid_level_defaults() -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    try:
        configure_logging("BOGUS")
        assert root.level == logging.INFO
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)
