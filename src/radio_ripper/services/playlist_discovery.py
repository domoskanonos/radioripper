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


def _match_keywords(
    entries: list[M3uEntry], keywords: list[str]
) -> list[tuple[M3uEntry, set[str]]]:
    """Return (entry, set_of_matched_keywords) for entries matching any keyword."""
    if not keywords:
        return [(e, set()) for e in entries]
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    if not lowered:
        return [(e, set()) for e in entries]
    result: list[tuple[M3uEntry, set[str]]] = []
    for e in entries:
        text = (e.name + " " + e.extinf).lower()
        matched: set[str] = set()
        for kw in lowered:
            if kw in text:
                matched.add(kw)
        if matched:
            result.append((e, matched))
    return result


def _distribute_probe_pool(
    matched: list[tuple[M3uEntry, set[str]]],
    keywords: list[str],
    max_needed: int,
) -> list[M3uEntry]:
    """Build a probe pool that gives each keyword a fair chance.

    Allocates up to ``ceil(max_needed / len(keywords))`` slots per keyword
    and round-robins entries so no single keyword dominates the probe.
    """
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    if not lowered or max_needed <= 0:
        return [e for e, _ in matched]

    per_keyword: dict[str, list[M3uEntry]] = {kw: [] for kw in lowered}
    for entry, matched_set in matched:
        for kw in matched_set:
            per_keyword[kw].append(entry)

    seen: set[str] = set()
    pool: list[M3uEntry] = []
    # Round-robin until we have enough or run out
    while len(pool) < max_needed:
        added = 0
        for kw in lowered:
            bucket = per_keyword[kw]
            remaining = [e for e in bucket if e.name.lower().strip() not in seen]
            if not remaining:
                continue
            entry = remaining.pop(0)
            # Rotate the bucket so we don't pick the same entry next round
            bucket.remove(entry)
            seen.add(entry.name.lower().strip())
            pool.append(entry)
            added += 1
            if len(pool) >= max_needed:
                break
        if added == 0:
            break

    for kw in lowered:
        count = sum(
            1 for e in pool
            if any(kw in matched for e2, matched in matched if e2.name == e.name)
        )
        if count < 5:
            _LOGGER.warning("Keyword '%s' has only %d station(s) in probe pool (< 5).", kw, count)

    return pool


def _keyword_coverage(
    good: list[tuple[M3uEntry, dict[str, Any]]],
    keywords: list[str],
) -> None:
    """Log per-keyword station counts after probing."""
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    for kw in lowered:
        text_key = kw
        count = sum(
            1 for entry, _ in good
            if text_key in (entry.name + " " + entry.extinf).lower()
        )
        if count < 5:
            _LOGGER.warning(
                "Keyword '%s' has only %d probed station(s) (< 5).", kw, count
            )
        else:
            _LOGGER.info("Keyword '%s': %d stations", kw, count)


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
    pending = set(tasks)

    while pending and len(ok) < max_ok:
        done_set, pending = await asyncio.wait(
            pending, timeout=3, return_when=asyncio.FIRST_COMPLETED
        )
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
        min_bps = self._settings.discovery_min_bitrate
        result: list[StreamConfig] = []
        for s in stations:
            url = str(s.url)
            if url in alive_map:
                probe = alive_map[url]
                if min_bps > 0 and probe.get("bitrate", 0) < min_bps:
                    continue
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

        keywords = self._settings.stream_keywords
        matched = _match_keywords(all_entries, keywords)
        filtered = [e for e, _ in matched]
        self._log.info("After keyword filter: %d entries", len(filtered))

        unique = _deduplicate_by_name(filtered)
        self._log.info("After dedup: %d unique stations", len(unique))

        if not unique:
            self._log.warning("No stations matched the configured keywords.")
            return []

        max_needed = self._settings.discovery_max_stations
        probe_pool = _distribute_probe_pool(matched, keywords, max_needed)

        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        self._log.info("Probing for ICY-capable streams (need %d, pool=%d)…", max_needed, len(probe_pool))
        good = await _probe_batch(probe_pool, max_needed, semaphore)
        self._log.info("Probing done: %d ICY-capable streams found", len(good))

        _keyword_coverage(good, keywords)

        min_bps = self._settings.discovery_min_bitrate
        if min_bps > 0:
            before = len(good)
            good = [(e, p) for e, p in good if p.get("bitrate", 0) >= min_bps]
            if len(good) < before:
                self._log.info(
                    "Filtered %d stations below %d kbps bitrate",
                    before - len(good),
                    min_bps,
                )

        good.sort(key=lambda x: x[1].get("bitrate", 0), reverse=True)

        stations: list[StreamConfig] = []
        for entry, probe in good[:max_needed]:
            try:
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
            except Exception as exc:
                _LOGGER.warning("Skipping %s: invalid config: %s", entry.name, exc)
        return stations


__all__ = [
    "PlaylistDiscoveryService",
]
