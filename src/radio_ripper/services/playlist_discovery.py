from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import HttpUrl

from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.infra.http import AsyncHttpClient

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


def _match_keywords(entries: list[M3uEntry], keywords: list[str]) -> list[tuple[M3uEntry, set[str]]]:
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


def _deduplicate_by_name(entries: list[M3uEntry]) -> list[M3uEntry]:
    seen: set[str] = set()
    result: list[M3uEntry] = []
    for e in entries:
        key = e.name.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(e)
    return result


def _distribute_probe_pool(
    matched: list[tuple[M3uEntry, set[str]]],
    keywords: list[str],
    max_needed: int,
) -> list[M3uEntry]:
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    if not lowered or max_needed <= 0:
        return [e for e, _ in matched]

    per_keyword: dict[str, list[M3uEntry]] = {kw: [] for kw in lowered}
    for entry, matched_set in matched:
        for kw in matched_set:
            per_keyword[kw].append(entry)

    seen: set[str] = set()
    pool: list[M3uEntry] = []
    while len(pool) < max_needed:
        added = 0
        for kw in lowered:
            bucket = per_keyword[kw]
            remaining = [e for e in bucket if e.name.lower().strip() not in seen]
            if not remaining:
                continue
            entry = remaining.pop(0)
            bucket.remove(entry)
            seen.add(entry.name.lower().strip())
            pool.append(entry)
            added += 1
            if len(pool) >= max_needed:
                break
        if added == 0:
            break

    for kw in lowered:
        count = sum(1 for e in pool if kw in (e.name + " " + e.extinf).lower())
        if count < 5:
            _LOGGER.warning("Keyword '%s' has only %d station(s) in probe pool (< 5).", kw, count)

    return pool


def _keyword_coverage(
    good: list[tuple[M3uEntry, dict[str, Any]]],
    keywords: list[str],
) -> None:
    lowered = [k.lower().strip() for k in keywords if k.strip()]
    for kw in lowered:
        count = sum(1 for entry, _ in good if kw in (entry.name + " " + entry.extinf).lower())
        if count < 5:
            _LOGGER.warning("Keyword '%s' has only %d probed station(s) (< 5).", kw, count)
        else:
            _LOGGER.info("Keyword '%s': %d stations", kw, count)


async def probe_icy(
    url: str,
    *,
    timeout: float = _PROBE_TIMEOUT,
    http_client: AsyncHttpClient | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"icy": False, "bitrate": 0, "error": None}
    headers = {"Icy-MetaData": "1", "User-Agent": "Radio-Ripper/2.0"}

    if http_client is not None:
        try:
            async for _ in http_client.stream_binary(url, headers=headers, timeout=timeout):
                break
            resp_headers = http_client.response_headers()
            metaint = resp_headers.get("icy-metaint") or resp_headers.get("Icy-Metaint")
            result["icy"] = metaint is not None
            br_raw = resp_headers.get("icy-br") or resp_headers.get("Icy-Br")
            if br_raw:
                with contextlib.suppress(ValueError, TypeError):
                    result["bitrate"] = int(br_raw)
        except Exception as exc:
            result["error"] = str(exc)[:60]
        return result

    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            ) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            if resp.status_code not in (200, 206):
                result["error"] = f"HTTP {resp.status_code}"
                return result
            resp_headers = dict(resp.headers)
            metaint = resp_headers.get("icy-metaint") or resp_headers.get("Icy-Metaint")
            result["icy"] = metaint is not None
            br_raw = resp_headers.get("icy-br") or resp_headers.get("Icy-Br")
            if br_raw:
                with contextlib.suppress(ValueError, TypeError):
                    result["bitrate"] = int(br_raw)
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
            probe = await probe_icy(entry.url)
            if probe["icy"]:
                return (entry, probe)
            return None

    tasks = [asyncio.create_task(_probe_one(e)) for e in entries]
    ok: list[tuple[M3uEntry, dict[str, Any]]] = []
    pending = set(tasks)

    while pending and len(ok) < max_ok:
        done_set, pending = await asyncio.wait(pending, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        for t in done_set:
            try:
                entry_data = t.result()
                if entry_data is not None:
                    ok.append(entry_data)
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
    _LOGGER.info("Downloaded ---everything-checked-repo.m3u (%.1f KiB, %.1fs)", len(text) / 1024, elapsed)
    return text


# ---------------------------------------------------------------- cache


def _cache_path(settings: Settings) -> Path:
    td = settings.temp_dir
    assert td is not None
    return td / "discovered_stations.m3u"


def _raw_mega_path(settings: Settings) -> Path:
    td = settings.temp_dir
    assert td is not None
    return td / "---everything-checked-repo.m3u"


def _load_cache(cache_file: Path) -> tuple[list[StreamConfig], str]:
    try:
        text = cache_file.read_text("utf-8")
        if text.strip().startswith("["):
            try:
                raw = json.loads(text)
                if isinstance(raw, list):
                    stations = [StreamConfig(**s) for s in raw if s.get("icy")]
                    return stations, ""
            except Exception:
                pass

        entries = _parse_m3u_text(text, cache_file.name)
        result: list[StreamConfig] = []
        for e in entries:
            try:
                result.append(
                    StreamConfig(
                        name=e.name,
                        url=HttpUrl(e.url),
                        enabled=True,
                        bitrate=0,
                        icy=True,
                        source=e.source,
                    )
                )
            except Exception:
                continue
        return result, ""
    except Exception:
        return [], ""


def _save_cache(cache_file: Path, stations: list[StreamConfig]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["#EXTM3U"]
    for s in stations:
        attr = []
        if getattr(s, "bitrate", 0):
            attr.append(f"bitrate={s.bitrate}")
        if getattr(s, "source", ""):
            attr.append(f"source={s.source}")
        attr_str = (" " + " ".join(attr)) if attr else ""
        lines.append(f"#EXTINF:-1{attr_str},{s.name}")
        lines.append(str(s.url))
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", "utf-8")
    tmp.replace(cache_file)


# ---------------------------------------------------------------- service


class PlaylistDiscoveryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = _LOGGER

    async def load_or_discover(self) -> list[StreamConfig]:
        if not self._settings.discovery_enabled:
            return []

        cache_file = _cache_path(self._settings)

        if cache_file.is_file():
            cached_stations, _ = _load_cache(cache_file)
            min_needed = self._settings.discovery_min_stations
            if cached_stations and len(cached_stations) >= min_needed:
                self._log.info("Using %d cached stations from %s", len(cached_stations), cache_file.name)
                return cached_stations
            self._log.info(
                "Cache has %d stations (need %d), re-discovering…",
                len(cached_stations),
                min_needed,
            )

        self._log.info("Starting discovery…")
        text = await self._load_or_download_mega()
        stations = await self._discover_from_text(text)
        if stations:
            _save_cache(cache_file, stations)
        self._log.info("Discovery complete: %d stations", len(stations))
        return stations

    async def _load_or_download_mega(self) -> str:
        raw_mega = _raw_mega_path(self._settings)
        if raw_mega.is_file():
            try:
                return raw_mega.read_text("utf-8")
            except Exception:
                pass
        pat = (
            self._settings.github_pat.get_secret_value()
            if self._settings.github_pat
            else os.environ.get("GITHUB_PAT", "")
        )
        text = await _download_mega_m3u(pat)
        try:
            raw_mega.parent.mkdir(parents=True, exist_ok=True)
            raw_mega.write_text(text, "utf-8")
        except Exception:
            pass
        return text

    async def _discover_from_text(self, text: str) -> list[StreamConfig]:
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

        min_needed = self._settings.discovery_min_stations
        probe_pool = _distribute_probe_pool(matched, keywords, len(unique))
        all_good: list[tuple[M3uEntry, dict[str, Any]]] = []
        remaining = probe_pool
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        batch_size = min(200, min_needed)

        while remaining and len(all_good) < min_needed:
            batch = remaining[:batch_size]
            remaining = remaining[batch_size:]
            self._log.info(
                "Probing batch of %d (have %d / need %d)…",
                len(batch),
                len(all_good),
                min_needed,
            )
            good = await _probe_batch(batch, min_needed - len(all_good), semaphore)
            all_good.extend(good)

        self._log.info("Probing done: %d ICY-capable streams found", len(all_good))

        _keyword_coverage(all_good, keywords)

        min_bps = self._settings.discovery_min_bitrate
        if min_bps > 0:
            before = len(all_good)
            all_good = [(e, p) for e, p in all_good if p.get("bitrate", 0) >= min_bps]
            if len(all_good) < before:
                self._log.info(
                    "Filtered %d stations below %d kbps bitrate",
                    before - len(all_good),
                    min_bps,
                )

        all_good.sort(key=lambda x: x[1].get("bitrate", 0), reverse=True)

        stations: list[StreamConfig] = []
        for entry, probe in all_good:
            try:
                stations.append(
                    StreamConfig(
                        name=entry.name[:64],
                        url=HttpUrl(entry.url),
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
    "M3uEntry",
    "PlaylistDiscoveryService",
    "probe_icy",
]
