"""M3U radio-stream discovery — fetch the mega M3U from GitHub.

Downloads the ``---everything-checked-repo.m3u`` file from
``junguler/m3u-radio-music-playlists``, parses the entries, filters by
user-defined keywords (matching both station name and #EXTINF attributes),
probes for ICY support + bitrate, and caches the top-N working stations
together with a hash of the active keywords so the cache is invalidated
when the keywords change.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from radio_ripper.infra.config import Settings, StreamConfig

_LOGGER = logging.getLogger("radio_ripper.discovery")
_MEGA_URL = (
    "https://raw.githubusercontent.com/junguler/m3u-radio-music-playlists"
    "/refs/heads/main/---everything-checked-repo.m3u"
)
_PROBE_TIMEOUT = 8.0
_MAX_CONCURRENT = 50


@dataclass(frozen=True)
class M3uEntry:
    name: str
    url: str
    source: str
    extinf: str = ""


def _keywords_hash(keywords: list[str]) -> str:
    h = hashlib.sha256()
    for k in sorted(keywords):
        h.update(k.lower().strip().encode())
    return h.hexdigest()[:16]


def _parse_m3u_text(text: str, source: str) -> list[M3uEntry]:
    entries: list[M3uEntry] = []
    current_name = ""
    current_extinf = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            current_extinf = line
            after_comma = line.split(",", 1)
            current_name = after_comma[1].strip() if len(after_comma) > 1 else ""
        elif line.startswith("#"):
            continue
        elif current_name:
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


def _filter_keywords(entries: list[M3uEntry], keywords: list[str]) -> list[M3uEntry]:
    if not keywords:
        return entries
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    if not lowered:
        return entries
    result: list[M3uEntry] = []
    for e in entries:
        text = (e.name + " " + e.extinf).lower()
        if any(kw in text for kw in lowered):
            result.append(e)
    return result


def _deduplicate_by_name(entries: list[M3uEntry]) -> list[M3uEntry]:
    seen: set[str] = set()
    result: list[M3uEntry] = []
    for e in entries:
        key = e.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(e)
    return result


async def _probe_icy(url: str, *, timeout: float = _PROBE_TIMEOUT) -> dict[str, Any]:
    result: dict[str, Any] = {"icy": False, "bitrate": 0, "error": None}
    headers = {"Icy-MetaData": "1", "User-Agent": "Radio-Ripper/2.0"}
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            ) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            if resp.status_code != 200 and resp.status_code != 206:
                result["error"] = f"HTTP {resp.status_code}"
                return result
            resp_headers = dict(resp.headers)
            metaint = resp_headers.get("icy-metaint") or resp_headers.get("Icy-Metaint")
            result["icy"] = metaint is not None
            br_raw = resp_headers.get("icy-br") or resp_headers.get("Icy-Br")
            if br_raw:
                with contextlib.suppress(ValueError, TypeError):
                    result["bitrate"] = int(br_raw)
            # Read one chunk to verify the stream actually sends data
            try:
                async for chunk in resp.aiter_bytes():
                    result["read_bytes"] = len(chunk)
                    break
            except Exception as exc:
                result["error"] = f"no data: {exc!s}"[:60]
                return result
    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connect"
    except httpx.RemoteProtocolError:
        result["error"] = "protocol"
    except Exception as exc:
        result["error"] = str(exc)[:60]
    return result


async def _probe_batch(
    entries: list[M3uEntry],
    max_ok: int,
    semaphore: asyncio.Semaphore,
) -> list[tuple[M3uEntry, dict[str, Any]]]:
    async def _probe_one(entry: M3uEntry) -> tuple[M3uEntry, dict[str, Any]] | None:
        async with semaphore:
            probe = await _probe_icy(entry.url)
            if probe["icy"]:
                return (entry, probe)
            return None

    tasks = [asyncio.create_task(_probe_one(e)) for e in entries]
    ok: list[tuple[M3uEntry, dict[str, Any]]] = []
    done: set[asyncio.Task[Any]] = set()
    pending = set(tasks)

    while pending and len(ok) < max_ok:
        done_set, pending = await asyncio.wait(
            pending, timeout=3, return_when=asyncio.FIRST_COMPLETED
        )
        done.update(done_set)
        for t in done_set:
            try:
                result = t.result()
                if result is not None:
                    ok.append(result)
            except Exception:
                pass

    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await t

    return ok


# ---------------------------------------------------------------- download


async def _download_mega_m3u(github_pat: str = "") -> str:
    """Download the ``---everything-checked-repo.m3u`` file and return its text."""
    headers: dict[str, str] = {"User-Agent": "Radio-Ripper/2.0"}
    if github_pat:
        headers["Authorization"] = f"Bearer {github_pat}"
    _LOGGER.info("Downloading ---everything-checked-repo.m3u…")
    t0 = time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    ) as client:
        resp = await client.get(_MEGA_URL, headers=headers)
        resp.raise_for_status()
        text = resp.text
    elapsed = time.monotonic() - t0
    _LOGGER.info(
        "Downloaded ---everything-checked-repo.m3u (%.1f KiB, %.1fs)", len(text) / 1024, elapsed
    )
    return text


# ---------------------------------------------------------------- cache


def _cache_path(settings: Settings) -> Path:
    return settings.temp_dir / "discovered_stations.json"


def _is_cache_fresh(cache_file: Path, max_age_days: int) -> bool:
    if not cache_file.is_file():
        return False
    return (time.time() - cache_file.stat().st_mtime) < max_age_days * 86400


def _load_cache(cache_file: Path) -> tuple[list[StreamConfig], str]:
    try:
        raw = json.loads(cache_file.read_text("utf-8"))
        if isinstance(raw, dict):
            stations = [StreamConfig(**s) for s in raw.get("stations", [])]
            kh = raw.get("_keywords_hash", "")
        else:
            stations = [StreamConfig(**s) for s in raw if s.get("icy")]
            kh = ""
        return stations, kh
    except Exception:
        return [], ""


def _save_cache(cache_file: Path, stations: list[StreamConfig], keywords_hash: str = "") -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "_keywords_hash": keywords_hash,
        "stations": [s.model_dump(mode="json") for s in stations],
    }
    cache_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


# ---------------------------------------------------------------- service


class PlaylistDiscoveryService:
    """Fetch the mega M3U, filter, probe, and cache stations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = _LOGGER

    async def load_or_discover(self) -> list[StreamConfig]:
        if not self._settings.discovery_enabled:
            return []

        cache_file = _cache_path(self._settings)
        kh = _keywords_hash(self._settings.stream_keywords)

        if _is_cache_fresh(cache_file, self._settings.discovery_update_interval_days):
            cached_stations, cached_hash = _load_cache(cache_file)
            if cached_stations and cached_hash == kh:
                self._log.info("Using %d cached stations (keywords match)", len(cached_stations))
                if self._settings.reprobe_on_start:
                    alive = await self._reprobe(cached_stations)
                    if alive:
                        _save_cache(cache_file, alive, keywords_hash=kh)
                    return alive or []
                return cached_stations

        self._log.info(
            "Starting playlist discovery (keywords=%s)…",
            self._settings.stream_keywords,
        )
        stations = await self._discover()
        _save_cache(cache_file, stations, keywords_hash=kh)
        self._log.info("Discovery complete: %d stations", len(stations))
        return stations

    async def _reprobe(self, stations: list[StreamConfig]) -> list[StreamConfig]:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        entries = [M3uEntry(name=s.name, url=str(s.url), source=s.source) for s in stations]
        good = await _probe_batch(entries, len(stations), semaphore)
        alive_map = {g[0].url: g[1] for g in good}
        result: list[StreamConfig] = []
        for s in stations:
            url = str(s.url)
            if url in alive_map:
                probe = alive_map[url]
                result.append(
                    StreamConfig(
                        name=s.name,
                        url=s.url,
                        enabled=s.enabled,
                        bitrate=probe.get("bitrate", s.bitrate),
                        icy=True,
                        source=s.source,
                    )
                )
        return result

    async def _discover(self) -> list[StreamConfig]:
        pat = self._settings.github_pat or os.environ.get("GITHUB_PAT", "")
        text = await _download_mega_m3u(pat)
        all_entries = _parse_m3u_text(text, "---everything-checked-repo.m3u")
        self._log.info("Parsed %d total M3U entries", len(all_entries))

        filtered = _filter_keywords(all_entries, self._settings.stream_keywords)
        self._log.info("After keyword filter: %d entries", len(filtered))

        unique = _deduplicate_by_name(filtered)
        self._log.info("After dedup: %d unique stations", len(unique))

        if not unique:
            self._log.warning("No stations matched the configured keywords.")
            return []

        random.shuffle(unique)
        max_needed = self._settings.discovery_max_stations
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

        self._log.info("Probing for ICY-capable streams (need %d)…", max_needed)
        good = await _probe_batch(unique, max_needed, semaphore)
        self._log.info("Probing done: %d ICY-capable streams found", len(good))

        good.sort(key=lambda x: x[1].get("bitrate", 0), reverse=True)

        stations: list[StreamConfig] = []
        for entry, probe in good[:max_needed]:
            stations.append(
                StreamConfig(
                    name=entry.name[:64],
                    url=entry.url,  # type: ignore[arg-type]
                    enabled=True,
                    bitrate=probe.get("bitrate", 0),
                    icy=True,
                    source=entry.source,
                )
            )
        return stations


__all__ = [
    "PlaylistDiscoveryService",
]
