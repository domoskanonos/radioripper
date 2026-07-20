"""Tests for radio_ripper.services.fingerprint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
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

    async def test_handles_acoustid_exception_gracefully(self) -> None:
        provider = AcoustidFingerprintProvider("test-key")
        with patch("acoustid.match", side_effect=RuntimeError("API down")):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_returns_none_when_artist_and_title_empty(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.0)
        fake_results = [(0.9, "rec123", "", "")]

        with patch("acoustid.match", return_value=fake_results):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None
