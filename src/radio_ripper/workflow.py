"""workflow.py — Einstiegspunkt & Orchestrierung des gesamten Ripping-Workflows.

Ablauf:
1. Aufräumen (.part-Reste)
2. Sender laden (custom.m3u)
3. ThreadPool (Größe = Sender-Anzahl, nur ffprobe)
4. AcoustID-Singleton-Worker starten
5. HTTP-Client + Recorder starten
6. Auf Stop-Signal warten
7. Shutdown: Recorder → Worker → Executor
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from radio_ripper.acoustid import AcoustidWorker
from radio_ripper.config import Settings, load_settings
from radio_ripper.http_client import HttpxClient
from radio_ripper.logging_setup import configure_logging
from radio_ripper.m3u import load_stations
from radio_ripper.recorder import StreamRecorder, cleanup_stale_parts

_LOGGER = logging.getLogger("radio_ripper.workflow")


async def _start_recorders(
    settings: Settings,
    client: HttpxClient,
    executor: ThreadPoolExecutor,
    acoustid_worker: AcoustidWorker | None = None,
) -> list[StreamRecorder]:
    """Startet einen Recorder pro aktiver Station."""
    stations = await load_stations(settings)
    recorders: list[StreamRecorder] = []
    for station in stations:
        rec = StreamRecorder(
            station=station,
            settings=settings,
            client=client,
            executor=executor,
            acoustid_worker=acoustid_worker,
        )
        rec.start()
        recorders.append(rec)
    _LOGGER.info("%d Recorder gestartet.", len(recorders))
    return recorders


async def run_stations(settings: Settings) -> None:
    """Startet das Streaming-Modul mit den übergebenen Settings."""
    cleanup_stale_parts(settings.work_dir)

    # ThreadPool-Größe = Anzahl der Sender (nur für ffprobe Länge/Größen-Test)
    stations = await load_stations(settings)
    pool_size = max(1, len(stations))
    executor = ThreadPoolExecutor(max_workers=pool_size)

    # AcoustID-Singleton-Worker (sequenziell, eigener asyncio-Task)
    acoustid_worker = AcoustidWorker(settings)
    acoustid_worker.start()

    async with HttpxClient(max_pool_size=pool_size) as client:
        recorders = await _start_recorders(settings, client, executor, acoustid_worker)

        stop_event = asyncio.Event()

        def _signal_handler(signum: int, _frame: object | None) -> None:
            _LOGGER.info("Signal %s empfangen — fahre herunter...", signum)
            stop_event.set()
            for rec in recorders:
                rec.stop()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _signal_handler, sig, None)

        try:
            await stop_event.wait()
        finally:
            for rec in recorders:
                rec.stop()
            await asyncio.gather(*(rec.join() for rec in recorders), return_exceptions=True)

    # AcoustID-Worker beenden (wartet auf Queue-Reste)
    await acoustid_worker.stop()
    executor.shutdown(wait=True)
    _LOGGER.info("Alle Recorder gestoppt. Tschüss!")


def main(argv: list[str] | None = None) -> int:
    """CLI-Einstiegspunkt für radio-ripper."""
    parser = argparse.ArgumentParser(
        prog="radio-ripper",
        description="Webradio-Stream-Recorder — dauerhafte parallele Aufzeichnung von ICY-Streams.",
    )
    parser.add_argument("-c", "--config", default="config/config.jsonc", help="Config-Datei (JSONC)")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log-Level überschreiben",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        settings = load_settings(args.config)
    except Exception as exc:
        print(f"Konnte Config nicht laden: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})

    # AcoustID-Key aus der Umgebung (ACOUST_ID) übernehmen, falls in Config nicht gesetzt
    api_key = os.environ.get("ACOUST_ID", "").strip()
    if api_key and not settings.acoustid_api_key:
        settings = settings.model_copy(update={"acoustid_api_key": api_key})

    configure_logging(settings.log_level, settings.work_dir / "streaming.log")

    try:
        asyncio.run(run_stations(settings))
    except KeyboardInterrupt:
        _LOGGER.info("KeyboardInterrupt — beendet.")
        return 0
    return 0
