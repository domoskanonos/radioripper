"""CLI entry point for radio-ripper stream."""

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
from radio_ripper.infra.logging import configure_logging

_LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radio-ripper-stream")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=None, help="Config file path")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser


async def _run(settings: Settings, logger: logging.Logger) -> int:
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
    logger.info("=== Radio-Ripper %s (stream mode) ===", __version__)

    try:
        return asyncio.run(_run(settings, logger))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shut down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
