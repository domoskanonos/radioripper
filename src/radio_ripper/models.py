"""models.py — Datenklassen für radio-ripper."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field, HttpUrl


@dataclass(frozen=True)
class M3uEntry:
    """Ein geparster M3U-Eintrag."""

    name: str
    url: str
    source: str = ""


@dataclass(frozen=True)
class AcoustidMatch:
    """Ergebnis eines AcoustID-Lookups."""

    artist: str
    title: str
    album: str = ""
    track_number: int | None = None
    year: int | None = None
    score: float = 0.0
    confirmations: int = 0
    recording_id: str = ""
    releasegroup_id: str = ""


class StreamConfig(BaseModel):
    """Konfiguration eines einzelnen Streams."""

    name: str = Field(min_length=1, max_length=64)
    url: HttpUrl
