"""Custom exception hierarchy for radio_ripper (stream)."""

from __future__ import annotations


class RadioRipperError(Exception):
    """Base error for every failure inside radio_ripper."""


class ConfigurationError(RadioRipperError):
    """Raised when the configuration file is missing, invalid, or incomplete."""


class StreamError(RadioRipperError):
    """Base error for any stream-related failure."""


class StreamConnectionError(StreamError):
    """Failed to connect to the stream URL (network, DNS, TLS, HTTP status)."""


class StreamProtocolError(StreamError):
    """The stream violated the expected ICY protocol (bad metaint, oversized metadata)."""


__all__ = [
    "ConfigurationError",
    "RadioRipperError",
    "StreamConnectionError",
    "StreamError",
    "StreamProtocolError",
]
