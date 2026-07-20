"""Tests for radio_ripper.services.fingerprint."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.domain.models import FingerprintResult
from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintError,
    NullFingerprintProvider,
)


class TestNullFingerprintProvider:
    async def test_always_returns_none(self) -> None:
        nfp = NullFingerprintProvider()
        result = await nfp.fingerprint(Path("/tmp/test.mp3"))
        assert result is None


class TestAcoustidFingerprintProvider:
    async def test_returns_match_for_good_result(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        fake_results = [(0.9, "rec123", "Test Artist", "Test Title")]

        with patch("acoustid.match", return_value=fake_results):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is not None
        assert result.artist == "Test Artist"
        assert result.title == "Test Title"
        assert result.score == 0.9
        assert result.recording_id == "rec123"

    async def test_returns_none_when_score_below_threshold(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.8)
        fake_results = [(0.5, "rec123", "Test Artist", "Test Title")]

        with patch("acoustid.match", return_value=fake_results):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is None

    async def test_returns_none_when_no_results(self) -> None:
        provider = AcoustidFingerprintProvider("test-key")
        with patch("acoustid.match", return_value=[]):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_raises_fingerprint_error_on_acoustid_exception(self) -> None:
        """Infrastructure failures (API down, network) must raise, NOT return None."""
        provider = AcoustidFingerprintProvider("test-key")
        with patch("acoustid.match", side_effect=RuntimeError("API down")):
            with pytest.raises(FingerprintError, match="acoustid lookup failed"):
                await provider.fingerprint(Path("/tmp/test.mp3"))

    async def test_raises_fingerprint_error_on_import_error(self) -> None:
        """Missing acoustid library is an infrastructure failure, not a no-match."""
        provider = AcoustidFingerprintProvider("test-key")
        # Force ImportError by injecting a sentinel that raises on attribute access
        original = sys.modules.get("acoustid")
        sys.modules["acoustid"] = None  # raise ImportError/TypeError on `import acoustid`
        try:
            with pytest.raises(FingerprintError, match="acoustid library not installed"):
                await provider.fingerprint(Path("/tmp/test.mp3"))
        finally:
            if original is not None:
                sys.modules["acoustid"] = original
            else:
                sys.modules.pop("acoustid", None)

    async def test_returns_none_when_artist_and_title_empty(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.0)
        fake_results = [(0.9, "rec123", "", "")]

        with patch("acoustid.match", return_value=fake_results):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_preserves_chained_exception_on_lookup_failure(self) -> None:
        """Ensure the original acoustid exception is chained for debugging."""
        provider = AcoustidFingerprintProvider("test-key")
        original_exc = ValueError("bad api key")
        with patch("acoustid.match", side_effect=original_exc):
            with pytest.raises(FingerprintError) as exc_info:
                await provider.fingerprint(Path("/tmp/test.mp3"))
        assert exc_info.value.__cause__ is original_exc