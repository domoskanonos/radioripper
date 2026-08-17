"""Tests für radio_ripper.silence — RmsTracker & SongBoundary."""

from __future__ import annotations

from radio_ripper.silence import RmsTracker, SongBoundary


def _fill_tracker(
    tracker: RmsTracker,
    start: float = 0.0,
    duration: float = 300.0,
    rms: float = -12.0,
    step: float = 0.5,
) -> None:
    """Füllt den Tracker mit konstantem RMS-Signal ab start."""
    t = start
    while t < start + duration:
        tracker.add_rms(t, rms)
        t += step


def test_song_boundary_fields() -> None:
    b = SongBoundary(time=10.5, level=-40.0, kind="silence")
    assert b.time == 10.5
    assert b.level == -40.0
    assert b.kind == "silence"


def test_no_boundary_on_constant_signal() -> None:
    """Konstantes Signal (ohne Einbrüche) → keine Grenzen."""
    tracker = RmsTracker()
    _fill_tracker(tracker, duration=300, rms=-12.0)
    assert tracker.boundaries == []


def test_stage1_absolute_silence() -> None:
    """RMS < -35dB über 1s → Grenze am Wiederaufnahmepunkt (kind=silence)."""
    tracker = RmsTracker(min_silence_s=1.0)
    # Normales Signal
    _fill_tracker(tracker, 0.0, 10.0, rms=-12.0)
    # Stille ab 10.0 (unter -35dB)
    _fill_tracker(tracker, 10.0, 2.5, rms=-50.0)
    # Wiederaufnahme ab 12.5
    _fill_tracker(tracker, 12.5, 2.0, rms=-12.0)

    assert len(tracker.boundaries) == 1
    b = tracker.boundaries[0]
    assert b.kind == "silence"
    # Grenze am Wiederaufnahmepunkt (12.5), nicht am Stille-Beginn (10.0)
    assert 12.4 <= b.time <= 13.0


def test_stage1_too_short_silence() -> None:
    """Silence kürzer als Mindestdauer → keine Grenze."""
    tracker = RmsTracker(min_silence_s=1.0)
    _fill_tracker(tracker, 0.0, 10.0, rms=-12.0)
    # nur 0.5s Stille
    _fill_tracker(tracker, 10.0, 0.5, rms=-50.0)
    _fill_tracker(tracker, 10.5, 2.0, rms=-12.0)
    assert tracker.boundaries == []


def test_stage2_dip() -> None:
    """Relativer Dip ≥8dB unter Median über 1s → Grenze (kind=dip)."""
    tracker = RmsTracker(dip_db=8.0, median_window=120, min_silence_s=1.0)
    # 60s normales Signal bei -12dB → Median = -12
    _fill_tracker(tracker, 0.0, 60.0, rms=-12.0)
    # Dip auf -22dB (10dB unter Median) für 2s
    _fill_tracker(tracker, 60.0, 2.0, rms=-22.0)
    # zurück auf Normal
    _fill_tracker(tracker, 62.0, 2.0, rms=-12.0)

    assert len(tracker.boundaries) == 1
    b = tracker.boundaries[0]
    assert b.kind == "dip"
    assert 61.9 <= b.time <= 63.0


def test_stage2_small_dip_no_boundary() -> None:
    """Dip unter 8dB → keine Grenze."""
    tracker = RmsTracker(dip_db=8.0)
    _fill_tracker(tracker, 0.0, 60.0, rms=-12.0)
    # nur -16dB (4dB unter Median)
    _fill_tracker(tracker, 60.0, 2.0, rms=-16.0)
    _fill_tracker(tracker, 62.0, 2.0, rms=-12.0)
    assert tracker.boundaries == []


def test_last_boundary_before() -> None:
    tracker = RmsTracker()
    tracker.boundaries.append(SongBoundary(time=10, level=-40, kind="silence"))
    tracker.boundaries.append(SongBoundary(time=20, level=-40, kind="silence"))
    assert tracker.last_boundary_before(15).time == 10  # type: ignore[union-attr]
    assert tracker.last_boundary_before(10) is None
    assert tracker.last_boundary_before(25).time == 20  # type: ignore[union-attr]


def test_boundaries_since() -> None:
    tracker = RmsTracker()
    tracker.boundaries.append(SongBoundary(time=10, level=-40, kind="silence"))
    tracker.boundaries.append(SongBoundary(time=20, level=-40, kind="silence"))
    assert [b.time for b in tracker.boundaries_since(15)] == [20]
    assert [b.time for b in tracker.boundaries_since(25)] == []


def test_adjacent_boundaries_merged() -> None:
    """Zwei Grenzen innerhalb 6s → nur eine bleibt."""
    tracker = RmsTracker(min_silence_s=0.5)
    _fill_tracker(tracker, 0.0, 5.0, rms=-12.0)
    _fill_tracker(tracker, 5.0, 1.5, rms=-50.0)  # Silence
    _fill_tracker(tracker, 6.5, 0.5, rms=-12.0)
    _fill_tracker(tracker, 7.0, 1.5, rms=-50.0)  # erneut Silence < 6s später
    _fill_tracker(tracker, 8.5, 1.0, rms=-12.0)
    assert len(tracker.boundaries) == 1
