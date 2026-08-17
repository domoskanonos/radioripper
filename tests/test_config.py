"""Tests für radio_ripper.config — Settings & JSONC-Laden."""

from __future__ import annotations

from pathlib import Path

import pytest

from radio_ripper.config import Settings, load_settings


def test_defaults() -> None:
    s = Settings()
    assert s.work_dir == Path("./work")
    assert s.destination == Path("./destination")
    assert s.acoustid_min_score == 0.9


def test_invalid_log_level() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(log_level="BOGUS")


def test_extra_fields_ignored() -> None:
    s = Settings.model_validate({"work_dir": "./w", "unbekannt": 123})
    assert s.work_dir == Path("./w")


def test_load_settings_jsonc_comments(tmp_path: Path) -> None:
    cfg = tmp_path / "config.jsonc"
    cfg.write_text('{\n  // Kommentar\n  "work_dir": "./rec",\n  "acoustid_min_score": 0.8 /* inline */\n}')
    s = load_settings(cfg)
    assert s.work_dir == Path("./rec")
    assert s.acoustid_min_score == 0.8


def test_load_settings_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.jsonc")
