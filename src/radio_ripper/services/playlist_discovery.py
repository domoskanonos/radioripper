"""M3U radio-stream discovery — download, parse, filter, probe, cache.

Fetches the jünguler/m3u-radio-music-playlists repo, parses the M3U files,
filters by user-defined keywords, probes for ICY metadata support and bitrate,
and caches the top-N working stations for the ripper to use.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import random
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

from radio_ripper.infra.config import Settings, StreamConfig

_LOGGER = logging.getLogger("radio_ripper.discovery")
_REPO_DIR = Path.home() / ".cache" / "radio-ripper" / "m3u-repo"
_CACHE_FILE = Path.home() / ".cache" / "radio-ripper" / "discovered_stations.json"
_PROBE_TIMEOUT = 8.0
_MAX_CONCURRENT = 50


@dataclass(frozen=True)
class M3uEntry:
    name: str
    url: str
    source: str


def _parse_m3u(path: Path) -> list[M3uEntry]:
    """Parse a single .m3u file and return list of (name, url, source)."""
    entries: list[M3uEntry] = []
    current_name = ""
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return entries
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            # #EXTINF:-1,Station Name  or  #EXTINF:-1 tvg-name="X",Station Name
            after_comma = line.split(",", 1)
            current_name = after_comma[1].strip() if len(after_comma) > 1 else ""
        elif line.startswith("#"):
            continue
        elif current_name:
            entries.append(M3uEntry(name=current_name, url=line, source=path.name))
            current_name = ""
    return entries


def _parse_all_m3us(repo_dir: Path) -> list[M3uEntry]:
    """Recursively parse all .m3u files under *repo_dir*."""
    entries: list[M3uEntry] = []
    for m3u in repo_dir.rglob("*.m3u"):
        entries.extend(_parse_m3u(m3u))
    return entries


def _filter_keywords(entries: list[M3uEntry], keywords: list[str]) -> list[M3uEntry]:
    """Keep entries whose name matches at least one keyword (case-insensitive)."""
    if not keywords:
        return entries
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    if not lowered:
        return entries
    result: list[M3uEntry] = []
    for e in entries:
        name_lower = e.name.lower()
        if any(kw in name_lower for kw in lowered):
            result.append(e)
    return result


def _deduplicate_by_name(entries: list[M3uEntry]) -> list[M3uEntry]:
    """Keep the first occurrence of each station name (case-insensitive)."""
    seen: set[str] = set()
    result: list[M3uEntry] = []
    for e in entries:
        key = e.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(e)
    return result


async def _probe_icy(url: str, *, timeout: float = _PROBE_TIMEOUT) -> dict:
    """Quick probe: check ICY support and bitrate for a stream URL.

    Returns dict with keys ``icy`` (bool), ``bitrate`` (int), ``error`` (str or None).
    """
    result: dict = {"icy": False, "bitrate": 0, "error": None}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url, headers={"Icy-MetaData": "1"}) as resp:
                if resp.status_code != 200 and resp.status_code != 206:
                    result["error"] = f"HTTP {resp.status_code}"
                    return result
                headers = dict(resp.headers)
                metaint = headers.get("icy-metaint") or headers.get("Icy-Metaint")
                result["icy"] = metaint is not None
                br_raw = headers.get("icy-br") or headers.get("Icy-Br")
                if br_raw:
                    try:
                        result["bitrate"] = int(br_raw)
                    except (ValueError, TypeError):
                        pass
                # Read a tiny bit to confirm it's really audio
                try:
                    await resp.areceive_headers()
                except Exception:
                    pass
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
) -> list[tuple[M3uEntry, dict]]:
    """Probe a batch of entries concurrently. Stops early when *max_ok* found."""

    async def _probe_one(entry: M3uEntry) -> tuple[M3uEntry, dict] | None:
        async with semaphore:
            probe = await _probe_icy(entry.url)
            if probe["icy"]:
                return (entry, probe)
            return None

    tasks = [asyncio.create_task(_probe_one(e)) for e in entries]
    ok: list[tuple[M3uEntry, dict]] = []
    done: set[asyncio.Task] = set()
    pending = set(tasks)

    while pending and len(ok) < max_ok:
        done_set, pending = await asyncio.wait(pending, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        done.update(done_set)
        for t in done_set:
            try:
                result = t.result()
                if result is not None:
                    ok.append(result)
            except Exception:
                pass

    # Cancel remaining
    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await t

    return ok


class PlaylistDiscoveryService:
    """Download the M3U repo, parse, filter, probe, and cache stations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = _LOGGER

    # ------------------------------------------------------------- public API

    async def load_or_discover(self) -> list[StreamConfig]:
        """Return cached stations if fresh, otherwise run full discovery."""
        if not self._settings.discovery_enabled:
            return []

        # Fresh enough?
        if _is_cache_fresh(_CACHE_FILE, self._settings.discovery_update_interval_days):
            cached = _load_cache(_CACHE_FILE)
            if cached:
                self._log.info(
                    "Using %d cached stations from %s", len(cached), _CACHE_FILE
                )
                if self._settings.reprobe_on_start:
                    alive = await self._reprobe(cached)
                    if len(alive) < len(cached):
                        self._log.info(
                            "Reprobe: %d/%d stations still alive",
                            len(alive), len(cached),
                        )
                    if len(alive) < self._settings.discovery_max_stations // 2:
                        self._log.info(
                            "Too few stations alive (%d), re-running full discovery…",
                            len(alive),
                        )
                        stations = await self._discover()
                        _save_cache(_CACHE_FILE, stations)
                        return stations
                    _save_cache(_CACHE_FILE, alive)
                    return alive
                return cached

        self._log.info("Starting playlist discovery (keywords=%s)…", self._settings.stream_keywords)
        stations = await self._discover()
        _save_cache(_CACHE_FILE, stations)
        self._log.info("Discovery complete: %d stations saved to %s", len(stations), _CACHE_FILE)
        return stations

    async def _reprobe(self, stations: list[StreamConfig]) -> list[StreamConfig]:
        """Re-probe cached stations and return only those still alive."""
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        entries = [
            M3uEntry(name=s.name, url=str(s.url), source=s.source)
            for s in stations
        ]
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
        repo_dir = await _ensure_repo(self._settings.discovery_repo_url)
        all_entries = _parse_all_m3us(repo_dir)
        self._log.info("Parsed %d total M3U entries", len(all_entries))

        filtered = _filter_keywords(all_entries, self._settings.stream_keywords)
        self._log.info("After keyword filter: %d entries", len(filtered))

        unique = _deduplicate_by_name(filtered)
        self._log.info("After dedup: %d unique stations", len(unique))

        if not unique:
            self._log.warning("No stations matched the configured keywords.")
            return []

        # Shuffle for variety, then probe
        random.shuffle(unique)
        max_needed = self._settings.discovery_max_stations
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

        self._log.info("Probing for ICY-capable streams (need %d)…", max_needed)
        good = await _probe_batch(unique, max_needed, semaphore)
        self._log.info("Probing done: %d ICY-capable streams found", len(good))

        # Sort by bitrate descending
        good.sort(key=lambda x: x[1].get("bitrate", 0), reverse=True)

        stations: list[StreamConfig] = []
        for entry, probe in good[:max_needed]:
            stations.append(
                StreamConfig(
                    name=entry.name[:64],
                    url=entry.url,
                    enabled=True,
                    bitrate=probe.get("bitrate", 0),
                    icy=True,
                    source=entry.source,
                )
            )
        return stations


# ------------------------------------------------------------------- helpers


def _is_cache_fresh(cache_file: Path, max_age_days: int) -> bool:
    if not cache_file.is_file():
        return False
    import time

    age = time.time() - cache_file.stat().st_mtime
    return age < max_age_days * 86400


def _load_cache(cache_file: Path) -> list[StreamConfig]:
    try:
        data = json.loads(cache_file.read_text("utf-8"))
        return [StreamConfig(**s) for s in data if s.get("icy")]
    except Exception:
        return []


def _save_cache(cache_file: Path, stations: list[StreamConfig]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    data = [s.model_dump(mode="json") for s in stations]
    cache_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


async def _ensure_repo(repo_url: str) -> Path:
    """Clone or update the M3U repo. Returns path to repo root."""
    repo_dir = _REPO_DIR
    if repo_dir.is_dir():
        # Update existing
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                _LOGGER.warning("git pull failed: %s", result.stderr[:200])
        except Exception as exc:
            _LOGGER.warning("git pull error: %s", exc)
    else:
        # Fresh clone
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(repo_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git clone failed: {result.stderr[:200]}")
        except Exception as exc:
            raise RuntimeError(f"cannot clone repo: {exc}") from exc
    return repo_dir


__all__ = [
    "PlaylistDiscoveryService",
]
