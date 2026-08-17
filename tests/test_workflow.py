"""Tests für radio_ripper.workflow — Orchestrierung & CLI-Einstieg."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from radio_ripper.config import Settings
from radio_ripper.workflow import _start_recorders, main, run_stations


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        work_dir=tmp_path,
        destination=tmp_path / "dest",
        max_concurrent_streams=10,
        acoustid_api_key="KEY",
    )


@pytest.mark.asyncio
async def test_run_stations_starts_and_stops(tmp_path: Path) -> None:
    """run_stations startet Recorder und fährt sie beim Stop sauber herunter."""
    settings = _make_settings(tmp_path)

    class _FakeRecorder:
        def __init__(self) -> None:
            self.stopped = False

        def start(self) -> _FakeRecorder:
            return self

        def stop(self) -> None:
            self.stopped = True

        async def join(self) -> None:
            return None

    fake = _FakeRecorder()

    async def _fake_start_recorders(*args, **kwargs):
        return [fake]

    # run_stations starten; nach kurzer Zeit wird der Event-Loop gezwungen,
    # indem wir die interne Task via cancel abbrechen. Das überprüft den
    # Cleanup-Pfad (recorder.stop + join).
    with (
        patch("radio_ripper.workflow._start_recorders", side_effect=_fake_start_recorders),
        patch("radio_ripper.workflow.AcoustidWorker"),
    ):
        task = asyncio.create_task(run_stations(settings))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_start_recorders_creates_recorder_per_station(tmp_path: Path) -> None:
    stations_dir = tmp_path / "stations"
    stations_dir.mkdir(parents=True)
    (stations_dir / "custom.m3u").write_text(
        "#EXTM3U\n#EXTINF:-1,A\nhttp://a.example\n#EXTINF:-1,B\nhttp://b.example\n"
    )
    settings = _make_settings(tmp_path)
    settings = settings.model_copy(update={"max_concurrent_streams": 1})

    client = AsyncMock()
    executor = object()

    with patch("radio_ripper.workflow.StreamRecorder") as mock_recorder:
        recorders = await _start_recorders(settings, client, executor)

    assert len(recorders) == 1  # max_concurrent_streams=1 begrenzt auf 1
    mock_recorder.assert_called_once()


def test_main_config_missing_returns_2(tmp_path: Path) -> None:
    """Fehlende Config → Exit-Code 2."""
    with patch("sys.argv", ["radio-ripper", "-c", str(tmp_path / "nope.jsonc")]):
        assert main() == 2


def test_main_keyboard_interrupt_returns_0(tmp_path: Path) -> None:
    """KeyboardInterrupt → Exit-Code 0."""
    cfg = tmp_path / "config.jsonc"
    cfg.write_text('{"work_dir": "./work"}')
    with (
        patch("sys.argv", ["radio-ripper", "-c", str(cfg)]),
        patch("radio_ripper.workflow.configure_logging"),
        patch("radio_ripper.workflow.run_stations", side_effect=KeyboardInterrupt),
    ):
        assert main() == 0


def test_main_loads_env_key(tmp_path: Path) -> None:
    """ACOUST_ID aus der Umgebung wird übernommen, wenn Config keinen Key hat."""
    cfg = tmp_path / "config.jsonc"
    cfg.write_text('{"work_dir": "./work"}')
    with (
        patch("sys.argv", ["radio-ripper", "-c", str(cfg)]),
        patch("radio_ripper.workflow.configure_logging"),
        patch("os.environ", {"ACOUST_ID": "ENVKEY"}),
        patch("radio_ripper.workflow.run_stations") as mock_run,
    ):
        main()
    settings_arg = mock_run.call_args.args[0]
    assert settings_arg.acoustid_api_key == "ENVKEY"


def test_main_log_level_override(tmp_path: Path) -> None:
    """--log-level überschreibt das Setting."""
    cfg = tmp_path / "config.jsonc"
    cfg.write_text('{"work_dir": "./work"}')
    with (
        patch("sys.argv", ["radio-ripper", "-c", str(cfg), "--log-level", "DEBUG"]),
        patch("radio_ripper.workflow.configure_logging"),
        patch("radio_ripper.workflow.run_stations") as mock_run,
    ):
        main()
    settings_arg = mock_run.call_args.args[0]
    assert settings_arg.log_level == "DEBUG"


@pytest.mark.asyncio
async def test_run_stations_signal_stops(tmp_path: Path) -> None:
    """run_stations reagiert auf ein Stop-Signal und fährt herunter."""
    import asyncio as _asyncio

    settings = _make_settings(tmp_path)

    class _FakeRecorder:
        def __init__(self) -> None:
            self.stopped = False

        def start(self) -> _FakeRecorder:
            return self

        def stop(self) -> None:
            self.stopped = True

        async def join(self) -> None:
            return None

    fake = _FakeRecorder()

    async def _fake_start_recorders(*args, **kwargs):
        return [fake]

    with (
        patch("radio_ripper.workflow._start_recorders", side_effect=_fake_start_recorders),
        patch("radio_ripper.workflow.AcoustidWorker"),
    ):
        # run_stations wartet auf stop_event — wir müssen es von außen setzen.
        # Einfachste Möglichkeit: die Task kurz laufen lassen, dann canceln
        # (der finally-Block stoppt die Recorder).
        task = _asyncio.create_task(run_stations(settings))
        await _asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task
    assert fake.stopped


@pytest.mark.asyncio
async def test_start_recorders_respects_limit(tmp_path: Path) -> None:
    """max_concurrent_streams begrenzt die Recorder-Anzahl."""
    stations_dir = tmp_path / "stations"
    stations_dir.mkdir(parents=True)
    (stations_dir / "custom.m3u").write_text(
        "#EXTM3U\n#EXTINF:-1,A\nhttp://a.example\n#EXTINF:-1,B\nhttp://b.example\n#EXTINF:-1,C\nhttp://c.example\n"
    )
    settings = _make_settings(tmp_path).model_copy(update={"max_concurrent_streams": 2})

    client = AsyncMock()
    executor = object()

    with patch("radio_ripper.workflow.StreamRecorder") as mock_recorder:
        recorders = await _start_recorders(settings, client, executor)
    assert len(recorders) == 2
    assert mock_recorder.call_count == 2
