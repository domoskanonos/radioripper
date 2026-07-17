"""Gradio GUI package for Radio-Ripper.

Entry point: :func:`radio_ripper.gui.main` (registered as ``radio-ripper-gui``).
The GUI is kept thin — it delegates all business logic to the
:mod:`radio_ripper.api` layer.
"""

from __future__ import annotations

from radio_ripper.gui.gui import build_app, main

__all__ = ["build_app", "main"]
