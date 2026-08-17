"""Tests für radio_ripper.acoustid — Tagging, Ordnerstruktur, Collision."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.acoustid import (
    build_metadata_filename,
    build_target_path,
    finalize_acoustid,
    read_mp3_score,
    write_mp3_tags,
)
from radio_ripper.config import Settings
from radio_ripper.models import AcoustidMatch


def test_build_metadata_filename() -> None:
    assert build_metadata_filename("AC/DC", "Highway to Hell") == "ACDC - Highway to Hell.mp3"


def test_build_target_path_with_album(tmp_path: Path) -> None:
    target = build_target_path(tmp_path, "Queen", "Bo Rhap", "A Night at the Opera")
    assert str(target.relative_to(tmp_path)) == "Queen/A Night at the Opera/Queen - Bo Rhap.mp3"


def test_build_target_path_without_album(tmp_path: Path) -> None:
    target = build_target_path(tmp_path, "Queen", "Bo Rhap")
    assert str(target.relative_to(tmp_path)) == "Queen/Queen - Bo Rhap.mp3"


def test_build_target_path_unknown_artist(tmp_path: Path) -> None:
    target = build_target_path(tmp_path, "", "Song")
    assert target.parent.name == "Unknown Artist"


def test_write_and_read_mp3_tags(tmp_path: Path) -> None:
    mp3 = tmp_path / "tag.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)

    write_mp3_tags(
        mp3,
        artist="Artist",
        title="Title",
        album="Album",
        score=0.95,
        confirmations=7,
        recording_id="rec-id",
        releasegroup_id="rg-id",
    )
    assert read_mp3_score(mp3) == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_finalize_acoustid_no_match_moves_to_unmatched(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x" * 100)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    with patch("radio_ripper.acoustid.acoustid_lookup", new=AsyncMock(return_value=(None, "ok"))):
        await finalize_acoustid(mp3, settings)
    assert not mp3.exists(), "Kein Treffer → Datei aus recordings/ verschoben"
    assert (tmp_path / "recordings" / "unmatched" / "rec.mp3").exists()


@pytest.mark.asyncio
async def test_finalize_acoustid_api_error_keeps(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x" * 100)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    with patch("radio_ripper.acoustid.acoustid_lookup", new=AsyncMock(return_value=(None, "error"))):
        await finalize_acoustid(mp3, settings)
    assert mp3.exists(), "API-Fehler → Datei bleibt erhalten"


@pytest.mark.asyncio
async def test_finalize_acoustid_match_moves_and_tags(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    match = AcoustidMatch(
        artist="Queen",
        title="Bo Rhap",
        album="Album",
        score=0.95,
        recording_id="rec-id",
        releasegroup_id="rg-id",
    )
    with patch("radio_ripper.acoustid.acoustid_lookup", new=AsyncMock(return_value=(match, "ok"))):
        await finalize_acoustid(mp3, settings)

    target = tmp_path / "dest" / "Queen" / "Album" / "Queen - Bo Rhap.mp3"
    assert target.exists(), "Treffer → Datei verschoben"
    assert read_mp3_score(target) == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_finalize_acoustid_collision_keeps_better_score(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    target = dest / "Artist" / "Artist - Song.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    write_mp3_tags(target, artist="Artist", title="Song", score=0.99)

    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    settings = Settings(work_dir=tmp_path, destination=dest, acoustid_api_key="KEY")

    match = AcoustidMatch(artist="Artist", title="Song", album="", score=0.90)
    with patch("radio_ripper.acoustid.acoustid_lookup", new=AsyncMock(return_value=(match, "ok"))):
        await finalize_acoustid(mp3, settings)

    assert target.exists(), "Bessere bestehende Datei bleibt"
    assert not mp3.exists(), "Schlechtere neue Datei wird verworfen"


# ---------------------------------------------------------------------------
# _fpcalc_sync
# ---------------------------------------------------------------------------


def test_fpcalc_missing(tmp_path: Path) -> None:
    from radio_ripper.acoustid import _fpcalc_sync

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    with patch("radio_ripper.acoustid.shutil.which", return_value=None):
        assert _fpcalc_sync(f) is None


def test_fpcalc_run_error(tmp_path: Path) -> None:
    from radio_ripper.acoustid import _fpcalc_sync

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    with (
        patch("radio_ripper.acoustid.shutil.which", return_value="/usr/bin/fpcalc"),
        patch("radio_ripper.acoustid.subprocess.run", side_effect=Exception("boom")),
    ):
        assert _fpcalc_sync(f) is None


def test_fpcalc_nonzero_exit(tmp_path: Path) -> None:
    from radio_ripper.acoustid import _fpcalc_sync

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")

    class _Proc:
        returncode = 3
        stdout = b""

    with (
        patch("radio_ripper.acoustid.shutil.which", return_value="/usr/bin/fpcalc"),
        patch("radio_ripper.acoustid.subprocess.run", return_value=_Proc()),
    ):
        assert _fpcalc_sync(f) is None


def test_fpcalc_valid(tmp_path: Path) -> None:
    from radio_ripper.acoustid import _fpcalc_sync

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")

    class _Proc:
        returncode = 0
        stdout = b'{"duration": 120.0, "fingerprint": "ABC"}'

    with (
        patch("radio_ripper.acoustid.shutil.which", return_value="/usr/bin/fpcalc"),
        patch("radio_ripper.acoustid.subprocess.run", return_value=_Proc()),
    ):
        result = _fpcalc_sync(f)
    assert result is not None
    assert result["duration"] == 120.0


def test_fpcalc_non_dict_json(tmp_path: Path) -> None:
    from radio_ripper.acoustid import _fpcalc_sync

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")

    class _Proc:
        returncode = 0
        stdout = b"[1, 2, 3]"

    with (
        patch("radio_ripper.acoustid.shutil.which", return_value="/usr/bin/fpcalc"),
        patch("radio_ripper.acoustid.subprocess.run", return_value=_Proc()),
    ):
        assert _fpcalc_sync(f) is None


# ---------------------------------------------------------------------------
# acoustid_lookup (httpx/respx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_no_fpcalc_returns_error(tmp_path: Path) -> None:
    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    with patch("radio_ripper.acoustid._fpcalc_sync", return_value=None):
        match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is None
    assert status == "error"


@pytest.mark.asyncio
async def test_lookup_api_error(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(500))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is None
    assert status == "error"


@pytest.mark.asyncio
async def test_lookup_ok_no_match(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(
            return_value=httpx.Response(200, json={"status": "ok", "results": []})
        )
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is None
    assert status == "ok"


@pytest.mark.asyncio
async def test_lookup_ok_with_match(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    payload = {
        "status": "ok",
        "results": [
            {
                "score": 0.95,
                "recordings": [
                    {
                        "id": "rec-1",
                        "title": "Song",
                        "artists": [{"name": "Artist"}],
                        "releasegroups": [{"id": "rg-1", "title": "Album", "firstreleasedate": "1999-05-01"}],
                        "confirmations": 3,
                        "track_number": 2,
                    }
                ],
            }
        ],
    }
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json=payload))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert status == "ok"
    assert match is not None
    assert match.artist == "Artist"
    assert match.title == "Song"
    assert match.album == "Album"
    assert match.year == 1999
    assert match.track_number == 2
    assert match.confirmations == 3
    assert match.recording_id == "rec-1"
    assert match.releasegroup_id == "rg-1"


@pytest.mark.asyncio
async def test_lookup_low_score_ignored(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    payload = {
        "status": "ok",
        "results": [{"score": 0.5, "recordings": [{"id": "r", "title": "T", "artists": [{"name": "A"}]}]}],
    }
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json=payload))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is None
    assert status == "ok"


# ---------------------------------------------------------------------------
# AcoustidWorker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_processes_sequentially(tmp_path: Path) -> None:
    import asyncio as _asyncio

    from radio_ripper.acoustid import AcoustidWorker

    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")
    worker = AcoustidWorker(settings)
    worker.start()

    processed = []

    async def fake_finalize(path: Path, settings: Settings) -> None:
        processed.append(path.name)

    with patch("radio_ripper.acoustid.finalize_acoustid", side_effect=fake_finalize):
        for name in ["a.mp3", "b.mp3", "c.mp3"]:
            p = tmp_path / name
            p.write_bytes(b"x")
            worker.enqueue(p)
        await _asyncio.sleep(0.1)
        await worker.stop()

    assert processed == ["a.mp3", "b.mp3", "c.mp3"]


@pytest.mark.asyncio
async def test_worker_enqueue_after_stop_ignored(tmp_path: Path) -> None:
    from radio_ripper.acoustid import AcoustidWorker

    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")
    worker = AcoustidWorker(settings)
    worker.start()
    await worker.stop()
    p = tmp_path / "late.mp3"
    p.write_bytes(b"x")
    worker.enqueue(p)  # darf nicht crashen
    assert worker.pending >= 0


# ---------------------------------------------------------------------------
# finalize_acoustid — weitere Pfade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_no_api_key_keeps(tmp_path: Path) -> None:
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"x" * 100)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="")
    await finalize_acoustid(mp3, settings)
    assert mp3.exists(), "Ohne API-Key bleibt die Datei erhalten"


@pytest.mark.asyncio
async def test_finalize_collision_replaces_lower_score(tmp_path: Path) -> None:
    """Bestehende Datei mit niedrigerem Score wird durch die neue ersetzt."""
    dest = tmp_path / "dest"
    target = dest / "Artist" / "Artist - Song.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    write_mp3_tags(target, artist="Artist", title="Song", score=0.50)

    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    settings = Settings(work_dir=tmp_path, destination=dest, acoustid_api_key="KEY")

    match = AcoustidMatch(artist="Artist", title="Song", album="", score=0.95)
    with patch("radio_ripper.acoustid.acoustid_lookup", new=AsyncMock(return_value=(match, "ok"))):
        await finalize_acoustid(mp3, settings)

    assert target.exists(), "Neue Datei ersetzt alte"
    assert read_mp3_score(target) == pytest.approx(0.95)
    assert not mp3.exists(), "Quelldatei verschwunden"


@pytest.mark.asyncio
async def test_finalize_move_error_keeps_source(tmp_path: Path) -> None:
    """Fehler beim Verschieben → Quelldatei bleibt in recordings/."""
    mp3 = tmp_path / "rec.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")

    match = AcoustidMatch(artist="Artist", title="Song", album="", score=0.95)
    with (
        patch("radio_ripper.acoustid.acoustid_lookup", new=AsyncMock(return_value=(match, "ok"))),
        patch("radio_ripper.acoustid.move_to_destination", side_effect=OSError("no space")),
    ):
        await finalize_acoustid(mp3, settings)
    assert mp3.exists(), "Quelldatei bleibt bei Move-Fehler"


@pytest.mark.asyncio
async def test_lookup_keeps_best_score_and_confirmations(tmp_path: Path) -> None:
    """Der Treffer mit höchstem Score (bei Gleichstand: mehr Confirmations) gewinnt."""
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    payload = {
        "status": "ok",
        "results": [
            {"score": 0.90, "recordings": [{"id": "r1", "title": "Low", "artists": [{"name": "A"}]}]},
            {"score": 0.95, "recordings": [{"id": "r2", "title": "Best", "artists": [{"name": "B"}]}]},
        ],
    }
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json=payload))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert status == "ok"
    assert match is not None
    assert match.title == "Best"


@pytest.mark.asyncio
async def test_lookup_tie_break_by_confirmations(tmp_path: Path) -> None:
    """Bei gleichem Score gewinnt der Treffer mit mehr Bestätigungen."""
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    payload = {
        "status": "ok",
        "results": [
            {
                "score": 0.95,
                "recordings": [{"id": "r1", "title": "Few", "artists": [{"name": "A"}], "confirmations": 2}],
            },
            {
                "score": 0.95,
                "recordings": [{"id": "r2", "title": "Many", "artists": [{"name": "B"}], "confirmations": 50}],
            },
        ],
    }
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json=payload))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, _ = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is not None
    assert match.title == "Many"


def test_move_to_destination_exdev_fallback(tmp_path: Path) -> None:
    """Cross-Device (EXDEV) → Copy-Fallback statt Fehler."""
    import errno

    from radio_ripper.acoustid import move_to_destination

    src = tmp_path / "src.mp3"
    src.write_bytes(b"data")
    sub = tmp_path / "sub"
    sub.mkdir()
    dst = sub / "dst.mp3"

    def _raise_exdev(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    with patch("os.replace", side_effect=_raise_exdev):
        move_to_destination(src, dst)
    assert dst.read_bytes() == b"data"
    assert not src.exists()


def test_move_to_destination_other_error_raises(tmp_path: Path) -> None:
    """Andere OSError-Fehler werden weitergegeben."""
    from radio_ripper.acoustid import move_to_destination

    src = tmp_path / "src.mp3"
    src.write_bytes(b"data")
    dst = tmp_path / "dst.mp3"

    with patch("os.replace", side_effect=OSError(1, "permission")), pytest.raises(OSError):
        move_to_destination(src, dst)


# ---------------------------------------------------------------------------
# acoustid_lookup — Edge-Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_non_dict_response(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json={"status": "nope"}))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is None
    assert status == "error"


@pytest.mark.asyncio
async def test_lookup_invalid_score_and_confirmations(tmp_path: Path) -> None:
    """Ungültiger Score / Confirmations / Tracknummer werden übersprungen."""
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    payload = {
        "status": "ok",
        "results": [
            {"score": "invalid", "recordings": [{"id": "r1", "title": "T", "artists": [{"name": "A"}]}]},
            {
                "score": 0.95,
                "recordings": [
                    {
                        "id": "r2",
                        "title": "Good",
                        "artists": [{"name": "B"}],
                        "confirmations": "many",
                        "track_number": "x",
                    }
                ],
            },
        ],
    }
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json=payload))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert status == "ok"
    assert match is not None
    assert match.title == "Good"
    assert match.confirmations == 0
    assert match.track_number is None


@pytest.mark.asyncio
async def test_lookup_recording_without_artist_skipped(tmp_path: Path) -> None:
    """Recording ohne Artist/Title wird übersprungen."""
    import httpx
    import respx

    from radio_ripper.acoustid import acoustid_lookup

    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    fp = {"duration": 120.0, "fingerprint": "ABC"}
    payload = {
        "status": "ok",
        "results": [{"score": 0.95, "recordings": [{"id": "r1"}]}],
    }
    with respx.mock:
        respx.get("https://api.acoustid.org/v2/lookup").mock(return_value=httpx.Response(200, json=payload))
        with patch("radio_ripper.acoustid._fpcalc_sync", return_value=fp):
            match, status = await acoustid_lookup(f, api_key="K", min_score=0.9)
    assert match is None
    assert status == "ok"


def test_read_mp3_score_missing_or_invalid(tmp_path: Path) -> None:
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(b"not a real mp3")
    assert read_mp3_score(mp3) is None
    assert read_mp3_score(tmp_path / "missing.mp3") is None


@pytest.mark.asyncio
async def test_worker_pending(tmp_path: Path) -> None:
    from radio_ripper.acoustid import AcoustidWorker

    settings = Settings(work_dir=tmp_path, destination=tmp_path / "dest", acoustid_api_key="KEY")
    worker = AcoustidWorker(settings)
    worker.start()
    assert worker.pending == 0
    p = tmp_path / "a.mp3"
    p.write_bytes(b"x")
    worker.enqueue(p)
    assert worker.pending == 1
    await worker.stop()


# ---------------------------------------------------------------------------
# MusicBrainz-Anreicherung
# ---------------------------------------------------------------------------


def test_mb_enrichment_defaults() -> None:
    from radio_ripper.acoustid import MusicBrainzEnrichment

    e = MusicBrainzEnrichment(genres=["rock"], cover_data=None, artist_image=None)
    assert e.genres == ["rock"]
    assert e.cover_data is None
    assert e.lyrics == ""
    assert e.synced_lyrics == ""


@pytest.mark.asyncio
async def test_fetch_genres(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import _fetch_genres

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://musicbrainz.org/ws/2/release-group/rg-1").mock(
                return_value=httpx.Response(200, json={"genres": [{"name": "rock"}, {"name": "pop"}]})
            )
            assert await _fetch_genres(client, "rg-1") == ["rock", "pop"]


@pytest.mark.asyncio
async def test_fetch_genres_empty_id(tmp_path: Path) -> None:
    import httpx

    from radio_ripper.acoustid import _fetch_genres

    async with httpx.AsyncClient() as client:
        assert await _fetch_genres(client, "") == []


@pytest.mark.asyncio
async def test_fetch_cover_art(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import _fetch_cover_art

    async with httpx.AsyncClient(follow_redirects=True) as client:
        with respx.mock:
            respx.get("https://coverartarchive.org/release-group/rg-1").mock(
                return_value=httpx.Response(
                    200,
                    json={"images": [{"front": True, "thumbnails": {"500": "https://example/img500.jpg"}}]},
                )
            )
            respx.get("https://example/img500.jpg").mock(return_value=httpx.Response(200, content=b"JPEGDATA"))
            assert await _fetch_cover_art(client, "rg-1") == b"JPEGDATA"


@pytest.mark.asyncio
async def test_fetch_cover_art_no_front(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import _fetch_cover_art

    async with httpx.AsyncClient(follow_redirects=True) as client:
        with respx.mock:
            respx.get("https://coverartarchive.org/release-group/rg-1").mock(
                return_value=httpx.Response(200, json={"images": [{"front": False}]})
            )
            assert await _fetch_cover_art(client, "rg-1") is None


@pytest.mark.asyncio
async def test_fetch_lyrics_synced(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import _fetch_lyrics

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://lrclib.net/api/search").mock(
                return_value=httpx.Response(
                    200,
                    json=[{"plainLyrics": "text", "syncedLyrics": "[00:00.15] line"}],
                )
            )
            plain, synced = await _fetch_lyrics(client, "Queen", "Bo Rhap")
    assert plain == "text"
    assert synced == "[00:00.15] line"


@pytest.mark.asyncio
async def test_fetch_lyrics_empty(tmp_path: Path) -> None:
    import httpx

    from radio_ripper.acoustid import _fetch_lyrics

    async with httpx.AsyncClient() as client:
        assert await _fetch_lyrics(client, "", "") == ("", "")
        assert await _fetch_lyrics(client, "X", "") == ("", "")


@pytest.mark.asyncio
async def test_enrich_musicbrainz(tmp_path: Path) -> None:
    import httpx
    import respx

    from radio_ripper.acoustid import enrich_musicbrainz
    from radio_ripper.models import AcoustidMatch

    match = AcoustidMatch(artist="Queen", title="Bo Rhap", releasegroup_id="rg-1", artist_id="ar-1")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        with respx.mock:
            respx.get("https://musicbrainz.org/ws/2/release-group/rg-1").mock(
                return_value=httpx.Response(200, json={"genres": [{"name": "rock"}]})
            )
            respx.get("https://coverartarchive.org/release-group/rg-1").mock(
                return_value=httpx.Response(200, json={"images": []})
            )
            respx.get("https://musicbrainz.org/ws/2/artist/ar-1").mock(
                return_value=httpx.Response(200, json={"relations": []})
            )
            respx.get("https://lrclib.net/api/search").mock(
                return_value=httpx.Response(200, json=[{"plainLyrics": "l", "syncedLyrics": ""}])
            )
            e = await enrich_musicbrainz(match, client)
    assert e.genres == ["rock"]
    assert e.lyrics == "l"


def test_add_synced_lyrics(tmp_path: Path) -> None:
    from mutagen.id3 import ID3, SYLT, ID3NoHeaderError

    from radio_ripper.acoustid import _add_synced_lyrics

    mp3 = tmp_path / "lyrics.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    try:
        audio = ID3(mp3)
    except ID3NoHeaderError:
        audio = ID3()
    _add_synced_lyrics(audio, "[00:00.15] line one\n[00:05.00] line two\n")
    audio.save(mp3)
    saved = ID3(mp3)
    sylt = [f for f in saved.values() if isinstance(f, SYLT)]
    assert len(sylt) == 1
    assert len(sylt[0].text) == 2  # type: ignore[attr-defined]


def test_write_mp3_tags_with_enrichment(tmp_path: Path) -> None:
    """Genre, Cover, Artist-Bild und Lyrics werden geschrieben."""
    from mutagen.id3 import ID3

    mp3 = tmp_path / "tag.mp3"
    mp3.write_bytes(b"\xff\xe0\x90\x00" + b"\x00" * 200)
    write_mp3_tags(
        mp3,
        artist="Artist",
        title="Title",
        album="Album",
        score=0.95,
        genres=["Rock", "Pop"],
        cover_data=b"\xff\xd8\xff\xe0" + b"\x00" * 20,  # JPEG-Header
        artist_image=b"\xff\xd8\xff\xe0" + b"\x00" * 10,
        lyrics="plain lyrics text",
    )
    tags = ID3(mp3)
    assert tags.getall("TCON")
    assert tags.getall("APIC")
    assert tags.getall("USLT")
    assert read_mp3_score(mp3) == pytest.approx(0.95)
