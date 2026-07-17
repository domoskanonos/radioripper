"""CLI entry point for radio_ripper.

Parses arguments, loads :class:`~radio_ripper.infra.config.Settings`,
configures logging, creates a :class:`~radio_ripper.app.RadioRipperApp` and
runs it forever until SIGINT/SIGTERM trigger a graceful shutdown.
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
from radio_ripper.app import RadioRipperApp
from radio_ripper.infra.config import Settings, load_settings
from radio_ripper.infra.errors import ConfigurationError
from radio_ripper.infra.logging import configure_logging

_DEFAULT_CONFIG_PATHS = (
    "./config.json",
    "~/.config/radio_ripper/config.json",
    "/etc/radio_ripper/config.json",
)


def _find_config_path(arg: str | None) -> str | None:
    if arg:
        return arg
    for candidate in _DEFAULT_CONFIG_PATHS:
        p = Path(candidate).expanduser()
        if p.is_file():
            return str(p)
    return None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radio-ripper",
        description=(
            "Production-grade Webradio-Ripper: dauerhaftes paralleles Aufzeichnen "
            "von ICY-Metadaten-Streams mit automatischer Song-Trennung, "
            "Duplikats-Erkennung (SQLite), ID3v2-Tagging (mutagen) und "
            "iTunes-basiertem Metadata-Enrichment inkl. Cover-Art."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiel:\n"
            "  uv run radio-ripper --config config.json\n"
            "  uv run radio-ripper --log-level DEBUG\n"
            "  uv run radio-ripper --no-enrich\n"
            "\n"
            "Stop mit Strg+C."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Pfad zur config.json (default: ./config.json, "
        "alternativ ~/.config/radio_ripper/config.json, /etc/radio_ripper/config.json).",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Ueberschreibt log_level aus der config.json.",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Schaltet iTunes-Enrichment & Cover-Download ab (override config).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


async def _run_async(settings: Settings, logger: logging.Logger) -> int:
    app = RadioRipperApp.from_settings(settings, logger=logger)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(signum: int, _frame: object | None) -> None:
        logger.info("Signal %s received - initiating graceful shutdown...", signum)
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _signal_handler, signal.SIGINT, None)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler, signal.SIGTERM, None)

    await app.start()
    try:
        await stop_event.wait()
    finally:
        await app.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv if argv is not None else sys.argv[1:])
    cfg_path = _find_config_path(args.config)
    if cfg_path is None or not Path(cfg_path).expanduser().is_file():
        print("No config found. Use --config PATH or create ./config.json", file=sys.stderr)
        return 2
    try:
        settings = load_settings(cfg_path)
    except ConfigurationError as exc:
        print(f"Failed to load config '{cfg_path}': {exc}", file=sys.stderr)
        return 2
    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})
    if args.no_enrich:
        settings = settings.model_copy(
            update={
                "enrich_metadata": False,
                "embed_cover_art": False,
            }
        )

    logger = configure_logging(settings.log_level, settings.log_file)
    logger.info("=== Radio-Ripper %s starting up ===", __version__)
    logger.info("Config file : %s", cfg_path)
    logger.info("Destination : %s", settings.destination)
    logger.info("Database    : %s", settings.database)
    logger.info("Streams     : %d", len(settings.streams))
    logger.info(
        "Enrichment  : metadata=%s cover_art=%s workers=%d",
        settings.enrich_metadata,
        settings.embed_cover_art,
        settings.enrichment_workers,
    )
    try:
        return asyncio.run(_run_async(settings, logger))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received - shutting down...")
        return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main"]
