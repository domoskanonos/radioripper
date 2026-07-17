"""Smoke tests for radio_ripper.gui.

We don't launch the server — we only verify that ``build_app`` constructs
a Gradio ``Blocks`` instance.

Gradio is an optional dependency. Tests are skipped if gradio is not
installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

gradio = pytest.importorskip("gradio")

from radio_ripper.gui.gui import build_app  # noqa: E402  # after importorskip


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "destination": str(tmp_path / "recordings"),
        "database": str(tmp_path / "songs.db"),
        "streams": [
            {"name": "TopHits", "url": "http://tophits.radiomonster.fm/listen.m3u"},
            {"name": "Rock", "url": "http://rock.radiomonster.fm/listen.m3u"},
        ],
    }), encoding="utf-8")
    return cfg


class TestBuildApp:
    def test_build_app_returns_blocks(self, tmp_config: Path) -> None:
        app = build_app(tmp_config)
        assert isinstance(app, gradio.Blocks)

    def test_build_app_with_invalid_config(self, tmp_path: Path) -> None:
        """App should still construct, falling back to default settings."""
        bad_config = tmp_path / "nonexistent.json"
        app = build_app(bad_config)
        assert isinstance(app, gradio.Blocks)