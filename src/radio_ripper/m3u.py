"""m3u.py — M3U-Parsing und Stationen laden."""

from __future__ import annotations

import logging
from pathlib import Path

from radio_ripper.config import Settings
from radio_ripper.models import M3uEntry, StreamConfig

_LOGGER = logging.getLogger("radio_ripper.m3u")


def parse_m3u_entries(text: str, source: str = "") -> list[M3uEntry]:
    """Parst M3U-Text in strukturierte Einträge."""
    entries: list[M3uEntry] = []
    current_name = ""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):
            after_comma = line.split(",", 1)
            current_name = after_comma[1].strip() if len(after_comma) > 1 else ""
            continue

        if line.startswith("#"):
            continue

        if current_name and "://" in line:
            entries.append(M3uEntry(name=current_name, url=line, source=source))
            current_name = ""

    return entries


async def load_stations(settings: Settings) -> list[StreamConfig]:
    """Lädt Stationen aus ``work_dir/stations/custom.m3u``."""
    custom_m3u = settings.work_dir / "stations" / "custom.m3u"
    if not custom_m3u.is_file():
        _LOGGER.warning("Keine custom.m3u gefunden: %s", custom_m3u)
        return []

    text = custom_m3u.read_text("utf-8")
    entries = parse_m3u_entries(text, source="custom")

    stations: list[StreamConfig] = []
    for e in entries:
        try:
            stations.append(StreamConfig(name=e.name[:64], url=e.url))
        except Exception as exc:
            _LOGGER.warning("Ungültiger Eintrag %s: %s", e.name, exc)

    _LOGGER.info("%d Stationen aus custom.m3u geladen", len(stations))
    return stations
