"""Tests for radio_ripper.infra.errors (stream)."""

from __future__ import annotations

from radio_ripper.infra.errors import (
    ConfigurationError,
    RadioRipperError,
    StreamConnectionError,
    StreamError,
    StreamProtocolError,
)


class TestHierarchy:
    def test_radio_ripper_error_is_base(self):
        assert issubclass(ConfigurationError, RadioRipperError)

    def test_stream_errors_inherit_stream_error(self):
        assert issubclass(StreamConnectionError, StreamError)
        assert issubclass(StreamProtocolError, StreamError)

    def test_stream_error_inherits_radio_ripper_error(self):
        assert issubclass(StreamError, RadioRipperError)

    def test_str_representation(self):
        assert str(StreamConnectionError("broken")) == "broken"
