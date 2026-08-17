"""silence.py — Live-Songgrenzen-Erkennung (3 Stufen) für radio-ripper.

Erkennt Songgrenzen anhand von:
  Stufe 1: Echte Stille (RMS unter absoluter Schwelle)
  Stufe 2: Relativer Dip (RMS deutlich unter dem gleitenden Median)
  Stufe 3: Zeit-Fallback (lange ohne Signal → ICY-Wechsel als Grenze)

Die Erkennung arbeitet live auf dem eingehenden Audio-Strom und meldet
Grenzen mit Zeitstempel. Der Recorder nutzt diese, um beim ICY-Titelwechsel
den exakten Songanfang/-ende aus einem Ring-Puffer zu schneiden.
"""

from __future__ import annotations

from collections import deque

# --- Schwellen (konfigurierbar) ---
ABSOLUTE_SILENCE_DB = -35.0  # Stufe 1: echte Stille
DIP_DB = 8.0  # Stufe 2: relativer Einbruch
MEDIAN_WINDOW = 120  # gleitender Median in 0.5s-Schritten (60s)
MIN_SILENCE_S = 1.0  # Mindestdauer einer Stille (Sekunden)
FALLBACK_AFTER_S = 180.0  # Stufe 3: nach dieser Zeit ohne Grenze


class SongBoundary:
    """Eine erkannte Songgrenze mit Zeitstempel."""

    __slots__ = ("kind", "level", "time")

    def __init__(self, time: float, level: float, kind: str) -> None:
        self.time = time
        self.level = level
        self.kind = kind  # "silence", "dip", "fallback"


class RmsTracker:
    """Live-RMS-Messung und 3-Stufen-Grenzen-Erkennung.

    Erhält fortlaufend RMS-Werte (in dB) mit Zeitstempel. Intern werden
    0.5s-Buckets geführt. Erkennt Songgrenzen und hält sie in einer Liste.
    """

    def __init__(
        self,
        *,
        absolute_silence_db: float = ABSOLUTE_SILENCE_DB,
        dip_db: float = DIP_DB,
        median_window: int = MEDIAN_WINDOW,
        min_silence_s: float = MIN_SILENCE_S,
        fallback_after_s: float = FALLBACK_AFTER_S,
    ) -> None:
        self._abs_db = absolute_silence_db
        self._dip_db = dip_db
        self._median_window = median_window
        self._min_silence_s = min_silence_s
        self._fallback_after = fallback_after_s

        # 0.5s-Buckets: (bucket_start, rms_durchschnitt)
        self._buckets: deque[tuple[float, float]] = deque()
        self._bucket_rms: deque[float] = deque()

        # Stufe-1-State (aktuelle Silence-Periode)
        self._in_silence = False
        self._silence_start = 0.0
        self._silence_min = 0.0

        # Stufe-2-State (letzter Dip)
        self._in_dip = False
        self._dip_start = 0.0
        self._dip_min = 0.0

        # Stufe-3-State
        self._last_boundary = 0.0

        self.boundaries: list[SongBoundary] = []

    # ------------------------------------------------------------------
    # Eingang
    # ------------------------------------------------------------------

    def add_rms(self, time: float, rms_db: float) -> None:
        """Füge einen RMS-Messwert hinzu (kann beliebig oft pro Sekunde sein)."""
        bucket = round(time * 2) / 2  # 0.5s-Raster
        if self._buckets and abs(self._buckets[-1][0] - bucket) < 0.01:
            # gleicher Bucket → Mittelwert erneuern
            _, old = self._buckets[-1]
            merged = (old + rms_db) / 2
            self._buckets[-1] = (bucket, merged)
            self._bucket_rms[-1] = merged
        else:
            self._buckets.append((bucket, rms_db))
            self._bucket_rms.append(rms_db)
            # Historie begrenzen (Median-Fenster + Puffer)
            if len(self._buckets) > self._median_window + 40:
                self._buckets.popleft()
                self._bucket_rms.popleft()

        self._evaluate(bucket, rms_db)

    # ------------------------------------------------------------------
    # Erkennung
    # ------------------------------------------------------------------

    def _evaluate(self, time: float, rms: float) -> None:
        self._check_stage1_silence(time, rms)
        self._check_stage2_dip(time, rms)
        self._check_stage3_fallback(time)

    def _check_stage1_silence(self, time: float, rms: float) -> None:
        """Echte Stille: RMS unter absoluter Schwelle für Mindestdauer.

        Die Grenze wird am *Wiederaufnahmepunkt* gesetzt (wenn der Pegel
        nach der Stille wieder ansteigt), damit sie den Songanfang markiert
        und nicht den Stille-Beginn.
        """
        if rms < self._abs_db:
            if not self._in_silence:
                self._in_silence = True
                self._silence_start = time
                self._silence_min = rms
            else:
                self._silence_min = min(self._silence_min, rms)
        elif self._in_silence:
            # Silence vorbei → war es lang genug? Grenze am Wiederaufnahmepunkt.
            self._in_silence = False
            dur = time - self._silence_start
            if dur >= self._min_silence_s:
                self._add_boundary(time, self._silence_min, "silence")

    def _check_stage2_dip(self, time: float, rms: float) -> None:
        """Relativer Dip: rms deutlich unter dem gleitenden Median.

        Grenze am Wiederaufnahmepunkt (Pegel steigt nach dem Dip wieder an).
        """
        if len(self._bucket_rms) < 30:
            return
        med = self._median(self._bucket_rms)
        if med - rms >= self._dip_db:
            if not self._in_dip:
                self._in_dip = True
                self._dip_start = time
                self._dip_min = rms
            else:
                self._dip_min = min(self._dip_min, rms)
        elif self._in_dip:
            self._in_dip = False
            dur = time - self._dip_start
            if dur >= self._min_silence_s:
                self._add_boundary(time, self._dip_min, "dip")

    def _check_stage3_fallback(self, time: float) -> None:
        """Kein Signal seit Fallback-Frist → merken (Recorder splittet am ICY)."""
        if self._last_boundary and time - self._last_boundary >= self._fallback_after:
            self._last_boundary = time  # nur einmal pro Frist melden

    def _add_boundary(self, time: float, level: float, kind: str) -> None:
        # Aufeinanderfolgende Boundaries derselben Art innerhalb 6s zusammenfassen
        if self.boundaries and abs(time - self.boundaries[-1].time) < 6.0:
            return
        self.boundaries.append(SongBoundary(time=time, level=level, kind=kind))
        self._last_boundary = time

    @staticmethod
    def _median(values: deque[float]) -> float:
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        mid = n // 2
        if n % 2 == 1:
            return sorted_vals[mid]
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2

    # ------------------------------------------------------------------
    # Abfrage
    # ------------------------------------------------------------------

    def last_boundary_before(self, time: float) -> SongBoundary | None:
        """Liefert die letzte Grenze strikt vor *time* (oder None)."""
        best: SongBoundary | None = None
        for b in self.boundaries:
            if b.time < time and (best is None or b.time > best.time):
                best = b
        return best

    def boundaries_since(self, time: float) -> list[SongBoundary]:
        return [b for b in self.boundaries if b.time >= time]
