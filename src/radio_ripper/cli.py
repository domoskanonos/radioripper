"""CLI entry point for radio_ripper — two subcommands.

``radio-ripper stream``
    Record MP3s from radio streams and dump raw files into
    ``work/streaming_results/``. No tagging, no enrichment.

``radio-ripper tag``
    Pick up raw MP3s from ``work/streaming_results/``, fingerprint, enrich,
    tag, fetch cover / lyrics, and move finished files to ``destination/``.
    Single-worker, one file at a time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from radio_ripper import __version__
from radio_ripper.infra.config import Settings, load_settings
from radio_ripper.infra.errors import ConfigurationError
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.infra.logging import configure_logging
from radio_ripper.services.fingerprint import AcoustidFingerprintProvider, NullFingerprintProvider
from radio_ripper.services.metadata import ITunesMetadataProvider
from radio_ripper.services.popularity import DeezerPopularityChecker
from radio_ripper.services.processor import FileProcessor
from radio_ripper.services.tagging import ID3Tagger

_LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radio-ripper")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    stream_p = sub.add_parser("stream", help="Stream radio stations and dump raw MP3s")
    stream_p.add_argument("-c", "--config", default=None, help="Config file path")
    stream_p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    stream_p.set_defaults(func=_run_stream)

    tag_p = sub.add_parser("tag", help="Process raw MP3s → enrich, tag, file in destination")
    tag_p.add_argument("-c", "--config", default=None, help="Config file path")
    tag_p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    tag_p.set_defaults(func=_run_tag)

    return parser


# ---------------------------------------------------------------------------
# stream — record only
# ---------------------------------------------------------------------------


async def _run_stream(settings: Settings, logger: logging.Logger) -> int:
    from radio_ripper.app import RadioRipperApp

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    app = RadioRipperApp.from_settings(settings, logger=logger)

    def _signal_handler(signum: int, _frame: object | None) -> None:
        logger.info("Signal %s received - shutting down...", signum)
        stop_event.set()
        app.cancel()

    loop.add_signal_handler(signal.SIGINT, _signal_handler, signal.SIGINT, None)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler, signal.SIGTERM, None)

    await app.start()
    try:
        await stop_event.wait()
    finally:
        await app.stop()
    return 0


# ---------------------------------------------------------------------------
# tag — process inbox
# ---------------------------------------------------------------------------


async def _run_tag(settings: Settings, logger: logging.Logger) -> int:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    client = HttpxAsyncClient(user_agent=settings.user_agent)

    api_key = (
        __import__("os").environ.get("ACOUSTID_API_KEY")
        or __import__("os").environ.get("ACCOUST_ID", "")
    )
    fp: AcoustidFingerprintProvider | NullFingerprintProvider
    if api_key:
        fp = AcoustidFingerprintProvider(api_key, min_score=settings.acoustid_min_score)
    else:
        logger.warning("ACOUSTID_API_KEY not set — fingerprinting disabled")
        fp = NullFingerprintProvider()

    metadata = ITunesMetadataProvider(client, metadata_timeout=settings.metadata_timeout)
    tagger = ID3Tagger()
    inbox = settings.mp3_inbox or settings.work_dir / "mp3_inbox"

    popularity: DeezerPopularityChecker | None = None
    if settings.min_popularity_rank and settings.min_popularity_rank > 0:
        popularity = DeezerPopularityChecker(client)

    proc = FileProcessor(
        inbox=inbox,
        temp_dir=settings.work_dir / "failed",
        settings=settings,
        fingerprint_provider=fp,
        metadata_provider=metadata,
        tagger=tagger,
        name="tag",
        poll_interval=2.0,
        cover_provider=None,  # TODO: add CoverArtArchiveProvider if wanted
        popularity_provider=popularity,
        logger=logger,
    )

    def _signal_handler(signum: int, _frame: object | None) -> None:
        logger.info("Signal %s received - shutting down...", signum)
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _signal_handler, signal.SIGINT, None)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler, signal.SIGTERM, None)

    await proc.start()
    try:
        await stop_event.wait()
    finally:
        await proc.stop()
        await client.aclose()
    return 0


# ---------------------------------------------------------------------------
# main dispatcher
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cfg_path: str | None = args.config
    if cfg_path is None or not Path(cfg_path).expanduser().is_file():
        print("No config found. Use --config PATH.", file=sys.stderr)
        return 2

    try:
        settings = load_settings(cfg_path)
    except ConfigurationError as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})

    logger = configure_logging(settings.log_level, settings.log_file)
    logger.info("=== Radio-Ripper %s (%s mode) ===", __version__, args.command)
    logger.info("Config     : %s", cfg_path)

    try:
        return asyncio.run(args.func(settings, logger))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shut down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
