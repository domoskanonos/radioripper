"""M3U radio-stream discovery — fetch ``+checked+`` playlists via GitHub API.

Lists all ``.m3u`` files in the ``+checked+`` directory of the
``junguler/m3u-radio-music-playlists`` repo, downloads them, parses the entries,
filters by user-defined keywords, probes for ICY support + bitrate, and caches
the top-N working stations together with a hash of the active keywords so the
cache is invalidated when the keywords change.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import httpx

from radio_ripper.infra.config import Settings, StreamConfig

_LOGGER = logging.getLogger("radio_ripper.discovery")
_API_URL = (
    "https://api.github.com/repos/junguler/m3u-radio-music-playlists/contents/%2Bchecked%2B"
)
_RAW_BASE = "https://raw.githubusercontent.com/junguler/m3u-radio-music-playlists/main/+checked+"
_PROBE_TIMEOUT = 8.0
_MAX_CONCURRENT = 50


@dataclass(frozen=True)
class M3uEntry:
    name: str
    url: str
    source: str


def _keywords_hash(keywords: list[str]) -> str:
    h = hashlib.sha256()
    for k in sorted(keywords):
        h.update(k.lower().strip().encode())
    return h.hexdigest()[:16]


def _parse_m3u_text(text: str, source: str) -> list[M3uEntry]:
    """Parse M3U content from a string and return entries."""
    entries: list[M3uEntry] = []
    current_name = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            after_comma = line.split(",", 1)
            current_name = after_comma[1].strip() if len(after_comma) > 1 else ""
        elif line.startswith("#"):
            continue
        elif current_name:
            entries.append(M3uEntry(name=current_name, url=line, source=source))
            current_name = ""
    return entries


def _filter_keywords(entries: list[M3uEntry], keywords: list[str]) -> list[M3uEntry]:
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
    seen: set[str] = set()
    result: list[M3uEntry] = []
    for e in entries:
        key = e.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(e)
    return result


async def _probe_icy(url: str, *, timeout: float = _PROBE_TIMEOUT) -> dict:
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

    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await t

    return ok


async def _list_m3u_files() -> list[str]:
    """List ``.m3u`` filenames in the ``+checked+`` repo directory."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        headers={"User-Agent": "Radio-Ripper/2.0", "Accept": "application/vnd.github.v3+json"},
    ) as client:
        resp = await client.get(_API_URL)
        resp.raise_for_status()
        items = resp.json()

    return [
        item["name"]
        for item in items
        if item["type"] == "file" and item["name"].lower().endswith(".m3u")
    ]


async def _fetch_m3u_content(filename: str) -> str:
    """Download a single M3U file content via raw GitHub."""
    url = f"{_RAW_BASE}/{filename}"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


# ---------------------------------------------------------------- cache


def _cache_path(settings: Settings) -> Path:
    return settings.temp_dir / "discovered_stations.json"


def _is_cache_fresh(cache_file: Path, max_age_days: int) -> bool:
    if not cache_file.is_file():
        return False
    import time
    return (time.time() - cache_file.stat().st_mtime) < max_age_days * 86400


def _load_cache(cache_file: Path) -> tuple[list[StreamConfig], str]:
    """Load cached stations and their keywords_hash."""
    try:
        raw = json.loads(cache_file.read_text("utf-8"))
        if isinstance(raw, dict):
            stations = [StreamConfig(**s) for s in raw.get("stations", [])]
            kh = raw.get("_keywords_hash", "")
        else:
            # legacy: flat list
            stations = [StreamConfig(**s) for s in raw if s.get("icy")]
            kh = ""
        return stations, kh
    except Exception:
        return [], ""


def _save_cache(
    cache_file: Path, stations: list[StreamConfig], keywords_hash: str = ""
) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "_keywords_hash": keywords_hash,
        "stations": [s.model_dump(mode="json") for s in stations],
    }
    cache_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


# ---------------------------------------------------------------- service


class PlaylistDiscoveryService:
    """Fetch ``+checked+`` M3U playlists, filter, probe, and cache stations."""

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
                self._log.info(
                    "Using %d cached stations (keywords match)", len(cached_stations)
                )
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
        self._log.info(
            "Discovery complete: %d stations", len(stations)
        )
        return stations

    async def _reprobe(self, stations: list[StreamConfig]) -> list[StreamConfig]:
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
        filenames = await _list_m3u_files()
        self._log.info("Found %d .m3u files in +checked+", len(filenames))

        contents = await asyncio.gather(
            *[_fetch_m3u_content(f) for f in filenames],
            return_exceptions=True,
        )

        all_entries: list[M3uEntry] = []
        for filename, text in zip(filenames, contents):
            if isinstance(text, Exception):
                self._log.debug("Failed to fetch %s: %s", filename, text)
                continue
            all_entries.extend(_parse_m3u_text(text, filename))

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
                    url=entry.url,
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
