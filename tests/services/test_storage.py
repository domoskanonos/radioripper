"""Tests for radio_ripper.services.storage (stream — TrackWriter only)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from radio_ripper.services.storage import (
    AcoustidMatch,
    TrackWriter,
    _extract_match_metadata,
    _parse_acoustid_response,
    build_metadata_filename,
    read_mp3_score,
    rename_track,
    sanitize_filename,
    split_icy_title,
    write_mp3_tags,
)


class TestSanitizeFilename:
    def test_strip_illegal_chars(self):
        assert sanitize_filename("A/B:C*D") == "ABCD"

    def test_replace_newlines_with_space(self):
        assert sanitize_filename("foo\r\nbar") == "foo bar"

    def test_collapse_whitespace(self):
        assert sanitize_filename("  foo   bar  ") == "foo bar"

    def test_truncate_long(self):
        long_name = "A" * 300
        result = sanitize_filename(long_name)
        assert len(result) <= 200

    def test_none_returns_empty(self):
        assert sanitize_filename(None) == ""

    def test_blank_returns_empty(self):
        assert sanitize_filename("  ") == ""

    def test_after_stripping_illegal_chars_returns_empty(self):
        assert sanitize_filename('<>:"') == ""


class TestTrackWriter:
    def test_initial_size_zero(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        assert w.size == 0

    def test_write_increases_size(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        w.write(b"hello")
        assert w.size == 5

    def test_commit_moves_file(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"data")
        assert w.commit() is True
        assert dst.is_file()
        assert dst.read_bytes() == b"data"

    def test_commit_below_min_size_discards(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=100)
        w.write(b"small")
        assert w.commit() is False
        assert not dst.exists()

    def test_commit_twice_noop(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"x")
        assert w.commit() is True
        assert w.commit() is False

    def test_discard_cleans_temp(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst)
        tmp = w._tmp_path
        assert tmp.exists()
        w.discard()
        assert not tmp.exists()

    def test_context_manager_commits(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        with TrackWriter(dst, min_size=1) as w:
            w.write(b"data")
        assert dst.is_file()

    def test_context_manager_discards_on_error(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        with pytest.raises(ValueError), TrackWriter(dst, min_size=1) as w:
            w.write(b"data")
            raise ValueError("boom")
        assert not dst.exists()


class TestGetMp3Duration:
    @patch("shutil.which", return_value=None)
    async def test_ffprobe_not_available(self, mock_which, tmp_path):
        from radio_ripper.services.storage import get_mp3_duration

        result = await get_mp3_duration(tmp_path)
        assert result is None

    async def test_duration_nonexistent_file(self, tmp_path):
        from radio_ripper.services.storage import get_mp3_duration

        result = await get_mp3_duration(tmp_path / "nonexistent.mp3")
        assert result is None


class TestIsValidMp3:
    async def test_valid_mp3_header(self, tmp_path):
        path = tmp_path / "test.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is True

    async def test_random_data_not_mp3(self, tmp_path):
        path = tmp_path / "test.bin"
        path.write_bytes(b"\x00\x01\x02\x03" + b"\xab" * 100)
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is False

    async def test_empty_file_not_mp3(self, tmp_path):
        path = tmp_path / "empty.mp3"
        path.write_bytes(b"")
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(path) is False

    async def test_nonexistent_file_returns_false(self, tmp_path):
        from radio_ripper.services.storage import is_valid_mp3

        assert await is_valid_mp3(tmp_path / "nope.mp3") is False


class TestTrackWriterAdvanced:
    def test_state_property(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        assert w.state == "open"
        w.write(b"x" * 100)
        w.commit()
        assert w.state == "committed"

    def test_flush(self, tmp_path):
        w = TrackWriter(tmp_path / "test.mp3")
        w.write(b"data")
        w.flush()
        assert w.size == 4

    def test_commit_flush_error_does_not_raise(self, tmp_path, caplog):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"data")
        with patch.object(w._fh, "flush", side_effect=OSError("disk error")):
            ok = w.commit()
        # Flush failure means the data could not be persisted — the file must
        # be discarded and reported as failed (never silently kept).
        assert ok is False
        assert not dst.exists()

    def test_discard_twice_noop(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst)
        w.discard()
        w.discard()

    def test_discard_after_commit_noop(self, tmp_path):
        dst = tmp_path / "out/test.mp3"
        w = TrackWriter(dst, min_size=1)
        w.write(b"data")
        w.commit()
        w.discard()
        assert dst.exists()


class TestGetMp3DurationAdvanced:
    async def test_duration_ffprobe_error(self, tmp_path):
        from radio_ripper.services.storage import get_mp3_duration

        path = tmp_path / "test.mp3"
        path.write_bytes(b"\x00" * 100)
        result = await get_mp3_duration(path)
        assert result is None or isinstance(result, float)


class TestParseAcoustidResponse:
    def _response(self, score: float, *, title: str = "Song", artist: str = "Artist") -> dict:
        return {
            "results": [
                {
                    "id": "r",
                    "score": score,
                    "recordings": [
                        {
                            "id": "rec",
                            "title": title,
                            "artists": [{"id": "a1", "name": artist}],
                        }
                    ],
                }
            ]
        }

    def test_selects_best_score(self):
        data = {
            "results": [
                {"id": "low", "score": 0.5, "recordings": [{"title": "Low", "artists": [{"name": "X"}]}]},
                {"id": "high", "score": 0.95, "recordings": [{"title": "Song", "artists": [{"name": "Artist"}]}]},
            ]
        }
        parsed = _parse_acoustid_response(data, min_score=0.9)
        assert parsed is not None
        score, result = parsed
        assert score == pytest.approx(0.95)
        assert result["id"] == "high"

    def test_below_threshold_returns_none(self):
        assert _parse_acoustid_response(self._response(0.5), min_score=0.9) is None

    def test_no_results_returns_none(self):
        assert _parse_acoustid_response({"status": "ok", "results": []}, min_score=0.9) is None

    def test_invalid_score_skipped(self):
        data = {"results": [{"id": "r", "score": "nope", "recordings": [{"title": "T", "artists": [{"name": "A"}]}]}]}
        assert _parse_acoustid_response(data, min_score=0.9) is None


class TestExtractMatchMetadata:
    def _recording(self, *, title: str = "Song", artists: list[dict] | None = None) -> dict:
        return {"recordings": [{"id": "rec", "title": title, "artists": artists or [{"name": "Artist"}]}]}

    def test_extracts_artist_and_title(self):
        match = _extract_match_metadata(self._recording(), score=0.95)
        assert match == AcoustidMatch(artist="Artist", title="Song", score=0.95)

    def test_joins_multiple_artists(self):
        match = _extract_match_metadata(self._recording(artists=[{"name": "A"}, {"name": "B"}]), score=0.9)
        assert match is not None
        assert match.artist == "A, B"

    def test_first_recording_with_metadata_wins(self):
        result = {
            "recordings": [
                {"id": "r1", "title": "", "artists": []},
                {"id": "r2", "title": "Song", "artists": [{"name": "Artist"}]},
            ]
        }
        match = _extract_match_metadata(result, score=0.95)
        assert match is not None
        assert match.title == "Song"

    def test_no_metadata_returns_none(self):
        assert _extract_match_metadata({"recordings": [{"id": "r1", "title": "", "artists": []}]}, score=0.95) is None

    def test_no_recordings_returns_none(self):
        assert _extract_match_metadata({"recordings": []}, score=0.95) is None


class TestAcoustidLookupMetadataRegression:
    async def test_high_score_without_metadata_is_rejected(self, tmp_path):
        """A qualifying score without usable metadata must reject the file."""
        from radio_ripper.services.storage import acoustid_lookup

        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        fp = json.dumps({"fingerprint": "fp", "duration": 120.0}).encode()
        api = json.dumps(
            {"status": "ok", "results": [{"id": "r", "score": 0.95, "recordings": [{"id": "rec"}]}]}
        ).encode()

        proc = AsyncMock()
        proc.communicate.return_value = (fp, b"")
        urlopen = Mock()
        urlopen.return_value.read.return_value = api
        with (
            patch("radio_ripper.services.storage.shutil.which", return_value="/usr/bin/fpcalc"),
            patch("radio_ripper.services.storage.asyncio.create_subprocess_exec", return_value=proc),
            patch("urllib.request.urlopen", urlopen),
        ):
            result = await acoustid_lookup(path, "key")

        # Strict policy: score without artist/title metadata cannot be kept
        assert result.outcome == "rejected"
        assert result.match is None

    async def test_result_without_recordings_is_rejected(self, tmp_path):
        """A qualifying result with no recordings block must reject the file."""
        from radio_ripper.services.storage import acoustid_lookup

        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        fp = json.dumps({"fingerprint": "fp", "duration": 120.0}).encode()
        api = json.dumps({"status": "ok", "results": [{"id": "r", "score": 0.95}]}).encode()

        proc = AsyncMock()
        proc.communicate.return_value = (fp, b"")
        urlopen = Mock()
        urlopen.return_value.read.return_value = api
        with (
            patch("radio_ripper.services.storage.shutil.which", return_value="/usr/bin/fpcalc"),
            patch("radio_ripper.services.storage.asyncio.create_subprocess_exec", return_value=proc),
            patch("urllib.request.urlopen", urlopen),
        ):
            result = await acoustid_lookup(path, "key")

        # Strict policy: no usable metadata means the file cannot be named/kept
        assert result.outcome == "rejected"
        assert result.match is None

    async def test_below_threshold_discards(self, tmp_path):
        from radio_ripper.services.storage import acoustid_lookup

        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        fp = json.dumps({"fingerprint": "fp", "duration": 120.0}).encode()
        api = json.dumps(
            {"status": "ok", "results": [{"id": "r", "score": 0.5, "recordings": [{"title": "T"}]}]}
        ).encode()

        proc = AsyncMock()
        proc.communicate.return_value = (fp, b"")
        urlopen = Mock()
        urlopen.return_value.read.return_value = api
        with (
            patch("radio_ripper.services.storage.shutil.which", return_value="/usr/bin/fpcalc"),
            patch("radio_ripper.services.storage.asyncio.create_subprocess_exec", return_value=proc),
            patch("urllib.request.urlopen", urlopen),
        ):
            result = await acoustid_lookup(path, "key")

        assert result.accepted is False
        assert result.match is None

    async def test_non_dict_response_is_error(self, tmp_path):
        """A malformed API response must return outcome='error' (strict fail-closed)."""
        from radio_ripper.services.storage import acoustid_lookup

        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        fp = json.dumps({"fingerprint": "fp", "duration": 120.0}).encode()

        proc = AsyncMock()
        proc.communicate.return_value = (fp, b"")
        urlopen = Mock()
        urlopen.return_value.read.return_value = b"[1, 2, 3]"
        with (
            patch("radio_ripper.services.storage.shutil.which", return_value="/usr/bin/fpcalc"),
            patch("radio_ripper.services.storage.asyncio.create_subprocess_exec", return_value=proc),
            patch("urllib.request.urlopen", urlopen),
        ):
            result = await acoustid_lookup(path, "key")

        # Strict fail-closed: malformed response is a transient error, not accepted
        assert result.outcome == "error"
        assert result.match is None


class TestSplitIcyTitle:
    def test_artist_title(self):
        assert split_icy_title("Artist - Title") == ("Artist", "Title")

    def test_no_separator(self):
        assert split_icy_title("Just a Song") == ("", "Just a Song")

    def test_multiple_separators(self):
        assert split_icy_title("A - B - C") == ("A", "B - C")

    def test_strips_whitespace(self):
        assert split_icy_title("  Artist  -  Title  ") == ("Artist", "Title")


class TestBuildMetadataFilename:
    def test_basic(self):
        assert build_metadata_filename("Artist", "Title") == "Artist - Title.mp3"

    def test_illegal_chars_stripped(self):
        assert build_metadata_filename("A/B", "Title") == "AB - Title.mp3"

    def test_empty_returns_empty(self):
        assert build_metadata_filename("", "") == ""
        assert build_metadata_filename("   ", "  ") == ""


class TestRenameTrack:
    def test_renames_file(self, tmp_path):
        src = tmp_path / "old - title.mp3"
        src.write_bytes(b"x")
        new = rename_track(src, "Artist", "Title")
        assert new.name == "Artist - Title.mp3"
        assert new.is_file()
        assert not src.exists()

    def test_no_rename_when_name_unchanged(self, tmp_path):
        src = tmp_path / "Artist - Title.mp3"
        src.write_bytes(b"x")
        assert rename_track(src, "Artist", "Title") == src

    def test_no_rename_when_empty_metadata(self, tmp_path):
        src = tmp_path / "song.mp3"
        src.write_bytes(b"x")
        assert rename_track(src, "", "") == src

    def test_renames_over_existing_target(self, tmp_path):
        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"keep")
        src = tmp_path / "other.mp3"
        src.write_bytes(b"new")
        new = rename_track(src, "Artist", "Title")
        assert new.name == "Artist - Title.mp3"
        assert new.is_file()
        assert new.read_bytes() == b"new"


class TestWriteMp3Tags:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert write_mp3_tags(path, artist="Artist", title="Title") is True

        from mutagen.id3 import ID3

        tags = ID3(path)
        assert str(tags["TPE1"]) == "Artist"
        assert str(tags["TIT2"]) == "Title"

    def test_missing_file_returns_false(self, tmp_path):
        assert write_mp3_tags(tmp_path / "nope.mp3", artist="A", title="T") is False

    def test_empty_fields_still_taggable(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert write_mp3_tags(path, artist="", title="") is True


class TestFinalizeWithMetadata:
    async def test_no_collision_renames_and_tags(self, tmp_path):
        from radio_ripper.services.storage import finalize_with_metadata

        src = tmp_path / "old.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()
        assert not src.exists()

        from mutagen.id3 import ID3

        tags = ID3(result)
        assert str(tags["TPE1"]) == "Artist"
        assert str(tags["TIT2"]) == "Title"

    async def test_existing_higher_score_is_kept(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key, **kwargs):
            return AcoustidLookup(outcome="accepted", match=AcoustidMatch("Artist", "Title", 0.99))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.9)
        assert result == existing
        assert existing.read_bytes() == b"old"
        assert not src.exists()

    async def test_existing_lower_score_is_replaced(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key, **kwargs):
            return AcoustidLookup(outcome="accepted", match=AcoustidMatch("Artist", "Title", 0.8))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()
        assert b"new" in result.read_bytes()
        assert not src.exists()

    async def test_tie_keeps_existing(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        async def fake_lookup(path, api_key, **kwargs):
            return AcoustidLookup(outcome="accepted", match=AcoustidMatch("Artist", "Title", 0.95))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result == existing
        assert not src.exists()

    async def test_existing_with_unknown_score_is_replaced(self, tmp_path, monkeypatch):
        """Policy change: unknown score on existing file → new recording replaces it."""
        from radio_ripper.services.storage import (
            AcoustidLookup,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"old")
        src = tmp_path / "new.mp3"
        src.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)

        async def fake_lookup(path, api_key, **kwargs):
            # Simulate API error for existing file — score unknown
            return AcoustidLookup(outcome="error", error_detail="timeout")

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        # New recording should replace the unscored existing file
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()

    async def test_existing_tag_score_used_without_relookup(self, tmp_path, monkeypatch):
        """A stored TXXX score avoids an API re-lookup of the existing file."""
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        write_mp3_tags(existing, artist="Artist", title="Title", score=0.99)
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        called = False

        async def fake_lookup(path, api_key, **kwargs):
            nonlocal called
            called = True
            return AcoustidLookup(outcome="accepted", match=AcoustidMatch("Artist", "Title", 0.0))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.9)
        assert result == existing
        assert not src.exists()
        assert called is False

    async def test_existing_tag_score_lower_replaces(self, tmp_path, monkeypatch):
        from radio_ripper.services.storage import (
            AcoustidLookup,
            AcoustidMatch,
            finalize_with_metadata,
        )

        existing = tmp_path / "Artist - Title.mp3"
        existing.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        write_mp3_tags(existing, artist="Artist", title="Title", score=0.8)
        src = tmp_path / "new.mp3"
        src.write_bytes(b"new")

        called = False

        async def fake_lookup(path, api_key, **kwargs):
            nonlocal called
            called = True
            return AcoustidLookup(outcome="accepted", match=AcoustidMatch("Artist", "Title", 0.0))

        monkeypatch.setattr("radio_ripper.services.storage.acoustid_lookup", fake_lookup)
        result = await finalize_with_metadata(src, "key", artist="Artist", title="Title", score=0.95)
        assert result.name == "Artist - Title.mp3"
        assert result.is_file()
        assert called is False


class TestReadMp3Score:
    def test_reads_written_score(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        write_mp3_tags(path, artist="Artist", title="Title", score=0.9123)
        assert read_mp3_score(path) == pytest.approx(0.9123)

    def test_untagged_returns_none(self, tmp_path):
        path = tmp_path / "song.mp3"
        path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert read_mp3_score(path) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert read_mp3_score(tmp_path / "nope.mp3") is None
