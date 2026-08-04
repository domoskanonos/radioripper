"""Unified M3U parsing utilities for radio_ripper."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class M3uEntry:
    """Represents a parsed M3U entry with metadata."""

    name: str
    url: str
    source: str = ""
    extinf: str = ""


def parse_m3u_entries(text: str, source: str = "") -> list[M3uEntry]:
    """
    Parse M3U text and return structured entries with metadata.

    Args:
        text: The M3U file content
        source: Source identifier (e.g., "custom", "discovery")

    Returns:
        List of M3uEntry objects with name, URL, and metadata

    Format:
        #EXTM3U
        #EXTINF:-1,Station Name
        http://stream.url
    """
    entries: list[M3uEntry] = []
    current_name = ""
    current_extinf = ""

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):
            current_extinf = line
            # Extract name after the last comma
            after_comma = line.split(",", 1)
            current_name = after_comma[1].strip() if len(after_comma) > 1 else ""
            continue

        if line.startswith("#"):
            continue

        # This is a URL line
        if current_name and "://" in line:
            entries.append(
                M3uEntry(
                    name=current_name,
                    url=line,
                    source=source,
                    extinf=current_extinf,
                )
            )
            current_name = ""
            current_extinf = ""

    return entries


def parse_m3u_urls(text: str) -> list[str]:
    """
    Parse M3U text and return only URLs (without metadata).

    Args:
        text: The M3U file content

    Returns:
        List of stream URLs
    """
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            urls.append(line)
    return urls


__all__ = [
    "M3uEntry",
    "parse_m3u_entries",
    "parse_m3u_urls",
]
