"""recorder.py — StreamRecorder: nimmt Radiostreams auf und validiert Tracks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from radio_ripper.acoustid import AcoustidWorker
from radio_ripper.config import Settings
from radio_ripper.http_client import HttpxClient, resolve_playlist
from radio_ripper.icy import AudioChunk, IcyParser, TitleChanged
from radio_ripper.models import StreamConfig
from radio_ripper.validation import validate_file
from radio_ripper.writer import TrackWriter, sanitize_filename

_LOGGER = logging.getLogger("radio_ripper.recorder")

_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0
_REQUEST_TIMEOUT = 30.0
_NO_ICY_DISABLE_AFTER = 10


def cleanup_stale_parts(work_dir: Path) -> int:
    """Entfernt übrig gebliebene ``.part``-Dateien aus abgebrochenen Läufen.

    ``.part``-Dateien sind unvollständige Aufnahmen (der atomare Rename zu
    ``.mp3`` fand nie statt) und werden nie weiterverarbeitet.
    """
    staging = work_dir / "recordings"
    if not staging.is_dir():
        return 0
    parts = sorted(staging.glob("*.part"))
    for part in parts:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
    if parts:
        _LOGGER.info("Entfernt %d unvollständige Aufnahme(n) (.part) aus einem früheren Lauf.", len(parts))
    return len(parts)


class StreamRecorder:
    """Nimmt einen einzelnen Radiostream auf und validiert jeden Track."""

    def __init__(
        self,
        *,
        station: StreamConfig,
        settings: Settings,
        client: HttpxClient,
        executor: ThreadPoolExecutor,
        acoustid_worker: AcoustidWorker | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.station = station
        self.settings = settings
        self._client = client
        self._executor = executor
        self._acoustid_worker = acoustid_worker
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._no_icy_failures = 0
        self._connect_failures = 0

    @property
    def station_name(self) -> str:
        return self.station.name

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        self._stop_event.set()

    async def join(self) -> None:
        if self._task is not None:
            await self._task

    def start(self) -> asyncio.Task[None]:
        self._task = asyncio.create_task(self._run_forever(), name=f"Recorder-{self.station.name}")
        return self._task

    # ------------------------------------------------------------------ core loop

    async def _run_forever(self) -> None:
        self._log.info(
            "Starte Recorder '%s' für '%s'",
            self.station.name,
            self.station.url,
        )
        delay = _RECONNECT_BASE_DELAY
        while not self._stop_event.is_set():
            try:
                ok = await self._run_once()
            except Exception:
                self._log.exception("Unerwarteter Fehler in Recorder '%s'", self.station.name)
                ok = False
            if self._stop_event.is_set():
                break
            if self._no_icy_failures >= _NO_ICY_DISABLE_AFTER:
                self._log.error(
                    "[%s] Deaktiviert: kein ICY-Metadaten nach %d Versuchen. Stream unterstützt vermutlich kein ICY.",
                    self.station.name,
                    self._no_icy_failures,
                )
                break
            if self._connect_failures >= _NO_ICY_DISABLE_AFTER:
                self._log.error(
                    "[%s] Deaktiviert: %d Verbindungsfehler in Folge.",
                    self.station.name,
                    self._connect_failures,
                )
                break
            if ok:
                delay = _RECONNECT_BASE_DELAY
            else:
                self._log.info(
                    "[%s] Reconnect in %.1fs (max %.1fs)",
                    self.station.name,
                    delay,
                    _RECONNECT_MAX_DELAY,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                delay = min(delay * 2.0, _RECONNECT_MAX_DELAY)
                delay *= 1.0 + random.random() * 0.5  # noqa: S311  -- Jitter gegen Thundering-Herd
        self._log.info("Recorder '%s' gestoppt.", self.station.name)

    async def _run_once(self) -> bool:
        try:
            urls = await resolve_playlist(
                self._client,
                str(self.station.url),
                timeout=_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            self._log.error("[%s] Playlist-Fehler: %s", self.station.name, exc)
            self._connect_failures += 1
            return False
        if not urls:
            self._log.error("[%s] Playlist enthielt keine Stream-URLs.", self.station.name)
            return False
        stream_url = urls[0]
        self._log.info("[%s] Verwende Stream-URL: %s", self.station.name, stream_url)
        try:
            ok = await self._stream_with_meta(stream_url)
            self._connect_failures = 0
            return ok
        except httpx.TimeoutException:
            self._log.error("[%s] Timeout beim Verbinden.", self.station.name)
            self._connect_failures += 1
            return False
        except httpx.HTTPError as exc:
            self._log.error("[%s] HTTP-Fehler: %s", self.station.name, exc)
            self._connect_failures += 1
            return False

    # ------------------------------------------------------------------ stream helpers

    async def _connect_stream(self, stream_url: str) -> tuple[AsyncGenerator[bytes, None], IcyParser] | None:
        headers = {"Icy-MetaData": "1"}
        agen = self._client.stream_binary(
            stream_url,
            headers=headers,
            timeout=_REQUEST_TIMEOUT,
        )
        try:
            first_chunk = await agen.__anext__()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await agen.aclose()
            self._log.warning(
                "[%s] Verbindung fehlgeschlagen: %s: %r",
                self.station.name,
                type(exc).__name__,
                exc,
            )
            raise
        resp_headers = self._client.response_headers()
        try:
            metaint = int(resp_headers.get("icy-metaint", 0))
        except (TypeError, ValueError):
            metaint = 0
        if not metaint or metaint <= 0:
            self._no_icy_failures += 1
            self._log.info(
                "[%s] Kein icy-metaint-Header; schließe. (Fehler %d/%d)",
                self.station.name,
                self._no_icy_failures,
                _NO_ICY_DISABLE_AFTER,
            )
            with contextlib.suppress(Exception):
                await agen.aclose()
            return None
        self._no_icy_failures = 0
        self._log.info("[%s] icy-metaint=%d", self.station.name, metaint)
        parser = IcyParser(metaint)
        parser.feed(first_chunk or b"")
        return agen, parser

    def _make_writer(self, icy_title: str) -> TrackWriter | None:
        """Erstellt einen TrackWriter im work_dir/recordings."""
        recordings_dir = self.settings.work_dir / "recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_filename(icy_title)
        if not safe_name:
            self._log.error("[%s] Kein Dateiname für Titel=%r", self.station.name, icy_title)
            return None

        file_path = recordings_dir / f"{safe_name}.mp3"
        try:
            return TrackWriter(file_path)
        except OSError as exc:
            self._log.error("[%s] Konnte %s nicht öffnen: %s", self.station.name, file_path, exc)
            return None

    def _should_record_title(self, title: str) -> bool:
        return bool(title.strip())

    # ------------------------------------------------------------------ main stream loop

    async def _stream_with_meta(self, stream_url: str) -> bool:
        connected = await self._connect_stream(stream_url)
        if connected is None:
            return False
        agen, parser = connected

        first_title_seen: str | None = None
        current_title: str | None = None
        writer: TrackWriter | None = None
        recording = False

        try:
            async for chunk in agen:
                if self._stop_event.is_set():
                    if writer is not None:
                        writer.discard()
                    return True
                if not chunk:
                    continue
                parser.feed(chunk)
                for event in parser.events():
                    if isinstance(event, AudioChunk):
                        if recording and writer is not None:
                            writer.write(event.data)
                    elif isinstance(event, TitleChanged):
                        new_title = event.title
                        if first_title_seen is None:
                            first_title_seen = new_title
                            current_title = new_title
                            self._log.info(
                                "[%s] Mitten im Song '%s' eingestiegen — warte auf nächste Grenze.",
                                self.station.name,
                                new_title,
                            )
                            continue
                        if new_title == current_title:
                            continue
                        if recording and writer is not None:
                            await self._finalize_writer(writer)
                            writer = None
                            recording = False
                        current_title = new_title
                        if not self._should_record_title(new_title):
                            continue
                        writer = self._make_writer(new_title.strip())
                        if writer is not None:
                            recording = True
                            self._log.info(
                                "[%s] Aufnahme -> %s",
                                self.station.name,
                                writer.final_path.name,
                            )
                        else:
                            recording = False
            self._log.info("[%s] Stream beendet (EOF).", self.station.name)
            if writer is not None:
                writer.discard()
            return True
        except Exception as exc:
            self._log.warning("[%s] Stream unterbrochen: %s", self.station.name, exc)
            if writer is not None:
                writer.discard()
            return False
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    async def _finalize_writer(self, writer: TrackWriter) -> None:
        """Committet den Track und führt beide Validierungs-Tests aus."""
        committed = writer.commit()
        if not committed:
            return

        final_path = writer.final_path
        ok = await validate_file(final_path, self.settings, self._executor)
        if not ok:
            return

        self._log.info(
            "[%s] Fertig (beide Tests bestanden): %s (%d bytes)",
            self.station.name,
            final_path.name,
            final_path.stat().st_size,
        )

        # AcoustID-Verarbeitung an den Singleton-Worker abgeben (blockiert nie)
        if self._acoustid_worker is not None:
            self._acoustid_worker.enqueue(final_path)
        else:
            self._log.info(
                "[%s] Kein AcoustID-Worker — Datei bleibt in recordings/: %s",
                self.station.name,
                final_path.name,
            )
