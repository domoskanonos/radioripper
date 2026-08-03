from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import HttpUrl

from radio_ripper.infra.config import Settings, StreamConfig
from radio_ripper.infra.errors import InvalidUrlError
from radio_ripper.infra.validation import validate_stream_url
from radio_ripper.services.m3u_parser import M3uEntry, parse_m3u_entries

# Backward-compatible alias
_parse_m3u_text = parse_m3u_entries

_LOGGER = logging.getLogger("radio_ripper.discovery")
_MEGA_URL = (
    "https://raw.githubusercontent.com/junguler/m3u-radio-music-playlists"
    "/refs/heads/main/---everything-checked-repo.m3u"
)


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
    timeout: float = 8.0,
) -> dict[str, Any]:
    result: dict[str, Any] = {"icy": False, "bitrate": 0, "error": None}

    # Validate URL before probing
    try:
        url = validate_stream_url(url)
    except InvalidUrlError as e:
        result["error"] = f"invalid URL: {e}"
        return result

    headers = {"Icy-MetaData": "1", "User-Agent": "Radio-Ripper/2.0"}

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
            ct = resp_headers.get("content-type", "").lower()
            if ct and ct != "audio/mpeg":
                result["error"] = f"not MP3 ({ct})"
                return result
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
    *,
    probe_timeout: float = 8.0,
) -> list[tuple[M3uEntry, dict[str, Any]]]:
    async def _probe_one(entry: M3uEntry) -> tuple[M3uEntry, dict[str, Any]] | None:
        async with semaphore:
            try:
                probe = await asyncio.wait_for(
                    probe_icy(entry.url, timeout=probe_timeout),
                    timeout=probe_timeout + 1.0,
                )
            except (TimeoutError, asyncio.CancelledError):
                return None
            if probe["icy"]:
                return (entry, probe)
            return None

    total = len(entries)
    log_interval = max(1, total // 10) if total > 10 else total
    last_logged = 0

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
        completed = total - len(pending)
        next_milestone = (last_logged // log_interval + 1) * log_interval
        if completed >= next_milestone:
            _LOGGER.info(
                "Probe progress: %d/%d (%.0f%%), %d OK",
                completed,
                total,
                completed / total * 100,
                len(ok),
            )
            last_logged = next_milestone

    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(asyncio.CancelledError):
            await t

    return ok


# ---------------------------------------------------------------- download


async def _download_mega_m3u() -> str:
    _LOGGER.info("Downloading ---everything-checked-repo.m3u…")
    t0 = time.monotonic()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=True,
    ) as client:
        resp = await client.get(_MEGA_URL, headers={"User-Agent": "Radio-Ripper/2.0"})
        resp.raise_for_status()
        text = resp.text
    elapsed = time.monotonic() - t0
    _LOGGER.info("Downloaded ---everything-checked-repo.m3u (%.1f KiB, %.1fs)", len(text) / 1024, elapsed)
    return text


# ---------------------------------------------------------------- cache

PREFILTERED_FILENAME = "prefiltered.m3u"
RANDOM_FILENAME = "random_stations.m3u"
FILTERED_FILENAME = "filtered_checked_stations.m3u"
WORK_FILENAME = "work_stations.m3u"


def _work_path(settings: Settings, filename: str = WORK_FILENAME) -> Path:
    """Generic path builder for work directory files."""
    return settings.work_dir / filename


def _cache_path(settings: Settings) -> Path:
    return _work_path(settings, "discovered_stations.m3u")


def _raw_mega_path(settings: Settings) -> Path:
    return _work_path(settings, "---everything-checked-repo.m3u")


def _prefiltered_path(settings: Settings) -> Path:
    return _work_path(settings, "prefiltered.m3u")


def _random_stations_path(settings: Settings) -> Path:
    return _work_path(settings, "random_stations.m3u")


def _filtered_path(settings: Settings) -> Path:
    return _work_path(settings, FILTERED_FILENAME)


_FINGERPRINT_PREFIX = "# radio-ripper-config: "


def _fingerprint_value(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _selection_fingerprint(settings: Settings) -> str:
    return _fingerprint_value(
        {
            "max_concurrent_streams": settings.max_concurrent_streams,
            "stream_keywords": list(settings.stream_keywords),
        }
    )


def _probe_fingerprint(settings: Settings) -> str:
    return _fingerprint_value(
        {
            "discovery_min_bitrate": settings.discovery_min_bitrate,
        }
    )


def _extract_fingerprint(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_FINGERPRINT_PREFIX):
            return stripped[len(_FINGERPRINT_PREFIX) :].strip()
    return ""


def _fingerprint_valid(actual: str, expected: str) -> bool:
    return actual == expected


def _load_cache(cache_file: Path) -> tuple[list[StreamConfig], str]:
    try:
        text = cache_file.read_text("utf-8")
        fingerprint = _extract_fingerprint(text)
        if text.strip().startswith("["):
            try:
                raw = json.loads(text)
                if isinstance(raw, list):
                    stations = [StreamConfig(**s) for s in raw if s.get("icy")]
                    return stations, ""
            except Exception:
                pass

        entries = parse_m3u_entries(text, source=cache_file.name)
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
        return result, fingerprint
    except Exception:
        return [], ""


def _save_cache(cache_file: Path, stations: list[StreamConfig], fingerprint: str = "") -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = ["#EXTM3U"]
        if fingerprint:
            lines.append(_FINGERPRINT_PREFIX + fingerprint)
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
    except Exception:
        _LOGGER.warning("Failed to save cache to %s", cache_file)


# ---------------------------------------------------------------- prefiltered


def _save_m3u_entries(path: Path, entries: list[M3uEntry]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U"]
        for e in entries:
            lines.append(e.extinf or f"#EXTINF:-1,{e.name}")
            lines.append(e.url)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", "utf-8")
        tmp.replace(path)
    except Exception:
        _LOGGER.warning("Failed to save M3U entries to %s", path)


def _save_prefiltered(
    path: Path,
    good: list[tuple[M3uEntry, dict[str, Any]]],
    fingerprint: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["#EXTM3U"]
    if fingerprint:
        lines.append(_FINGERPRINT_PREFIX + fingerprint)
    for entry, probe in good:
        br = probe.get("bitrate", 0) or 0
        lines.append(f"#EXTINF:-1 bitrate={br},{entry.name}")
        lines.append(entry.url)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", "utf-8")
    tmp.replace(path)


def _load_prefiltered(path: Path) -> list[tuple[M3uEntry, dict[str, Any]]]:
    if not path.is_file():
        return []
    try:
        text = path.read_text("utf-8")
    except Exception:
        return []
    entries = parse_m3u_entries(text, source=path.name)
    result: list[tuple[M3uEntry, dict[str, Any]]] = []
    for e in entries:
        br = 0
        m = re.search(r"bitrate=(\d+)", e.extinf)
        if m:
            br = int(m.group(1))
        result.append((e, {"icy": True, "bitrate": br, "error": None}))
    return result


# ---------------------------------------------------------------- service


class PlaylistDiscoveryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = _LOGGER

    async def load_or_discover(self) -> list[StreamConfig]:
        if not self._settings.discovery_enabled:
            return []

        expected_sel = _selection_fingerprint(self._settings)
        expected_probe = _probe_fingerprint(self._settings)

        work_file = _work_path(self._settings, WORK_FILENAME)
        if work_file.is_file():
            stations, fingerprint = _load_cache(work_file)
            if stations and _fingerprint_valid(fingerprint, expected_sel):
                self._log.info("Loaded %d stations from %s", len(stations), work_file.name)
                return stations
            self._log.info("Work cache %s is stale — rebuilding selection.", work_file.name)

        filtered_file = _filtered_path(self._settings)
        if not filtered_file.is_file():
            legacy = _prefiltered_path(self._settings)
            if legacy.is_file():
                filtered_file = legacy
                self._log.info("Found legacy %s, treating as filtered cache", legacy.name)

        if filtered_file.is_file():
            try:
                text = filtered_file.read_text("utf-8")
            except Exception:
                text = ""
            prefiltered = _load_prefiltered(filtered_file)
            fingerprint = _extract_fingerprint(text)
            if prefiltered and _fingerprint_valid(fingerprint, expected_probe):
                self._log.info("Loaded %d stations from %s", len(prefiltered), filtered_file.name)
                stations = self._select_from_prefiltered(prefiltered)
                if stations:
                    _save_cache(work_file, stations, expected_sel)
                    self._log.info("Saved %d stations to %s", len(stations), work_file.name)
                return stations
            if prefiltered:
                self._log.info("Filtered cache %s is stale — reprobing.", filtered_file.name)

        random_file = _random_stations_path(self._settings)
        if random_file.is_file():
            random_entries = parse_m3u_entries(random_file.read_text("utf-8"), source=random_file.name)
            self._log.info("Loaded %d random entries from %s", len(random_entries), random_file.name)
        else:
            text = await self._load_or_download_mega()
            all_entries = parse_m3u_entries(text, source="---everything-checked-repo.m3u")
            unique = _deduplicate_by_name(all_entries)
            self._log.info("Parsed %d unique entries from mega M3U", len(unique))
            random.shuffle(unique)
            random_entries = unique[: self._settings.discovery_random_sample_size]
            _save_m3u_entries(random_file, random_entries)
            self._log.info("Saved %d random entries to %s", len(random_entries), random_file.name)

        good = await self._probe_and_filter(random_entries)

        if good:
            _save_prefiltered(filtered_file, good, expected_probe)
            self._log.info("Saved %d stations to %s", len(good), filtered_file.name)

        stations = self._select_from_prefiltered(good)
        if stations:
            _save_cache(work_file, stations, expected_sel)
            self._log.info("Saved %d stations to %s", len(stations), work_file.name)

        return stations

    async def _load_or_download_mega(self) -> str:
        raw_mega = _raw_mega_path(self._settings)
        if raw_mega.is_file():
            try:
                return raw_mega.read_text("utf-8")
            except Exception:
                pass
        text = await _download_mega_m3u()
        try:
            raw_mega.parent.mkdir(parents=True, exist_ok=True)
            raw_mega.write_text(text, "utf-8")
        except Exception:
            pass
        return text

    async def _probe_and_filter(
        self,
        entries: list[M3uEntry],
    ) -> list[tuple[M3uEntry, dict[str, Any]]]:
        if not entries:
            return []

        semaphore = asyncio.Semaphore(self._settings.discovery_max_concurrent)
        total = len(entries)
        self._log.info("Probing %d stations…", total)
        good = await _probe_batch(
            entries,
            max_ok=total,
            semaphore=semaphore,
            probe_timeout=self._settings.discovery_probe_timeout,
        )
        self._log.info("Probing done: %d ICY-capable streams found", len(good))

        min_bps = self._settings.discovery_min_bitrate
        if min_bps > 0:
            before = len(good)
            good = [(e, p) for e, p in good if p.get("bitrate", 0) >= min_bps]
            filtered_count = before - len(good)
            if filtered_count:
                self._log.info(
                    "Filtered %d stations below %d kbps",
                    filtered_count,
                    min_bps,
                )

        good.sort(key=lambda x: x[1].get("bitrate", 0), reverse=True)
        return good

    def _select_from_prefiltered(
        self,
        good: list[tuple[M3uEntry, dict[str, Any]]],
    ) -> list[StreamConfig]:
        if not good:
            return []

        settings = self._settings
        keywords = settings.stream_keywords
        max_streams = settings.max_concurrent_streams

        if keywords:
            entries = [e for e, _ in good]
            matched = _match_keywords(entries, keywords)
            selected = [e for e, _ in matched]
            _keyword_coverage(good, keywords)
            self._log.info("Keyword filter: %d of %d stations", len(selected), len(good))
        else:
            pool = list(good)
            random.shuffle(pool)
            selected = [e for e, _ in pool]
            self._log.info("No keywords — random selection from %d prefiltered stations", len(selected))

        probe_by_url = {p[0].url: p[1] for p in good}
        stations: list[StreamConfig] = []
        for entry in selected[:max_streams]:
            probe = probe_by_url.get(entry.url, {"icy": True, "bitrate": 0, "error": None})
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
