"""acoustid.py — AcoustID-Pipeline: Fingerprint, Lookup, Tagging, Verschieben."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx

from radio_ripper.config import Settings
from radio_ripper.models import AcoustidMatch
from radio_ripper.writer import sanitize_filename

_LOGGER = logging.getLogger("radio_ripper.acoustid")

_ACOUSTID_API_URL = "https://api.acoustid.org/v2/lookup"
_MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
_COVERART_API = "https://coverartarchive.org"
_EXDEV = 18  # errno.EXDEV — Cross-Device-Link
_STOP_SENTINEL = Path("__stop__")
_ACOUSTID_SCORE_TAG = "ACOUSTID_SCORE"

_MB_UA = "radio-ripper/2.14.0 (https://github.com/domoskanonos/radioripper)"

# Serialisiert die Kollisionsprüfung + Verschieben, damit parallele Worker
# nie gleichzeitig auf dasselbe Ziel schreiben.
_FINALIZE_LOCK = threading.Lock()


def _fpcalc_sync(path: Path) -> dict[str, Any] | None:
    """Führt fpcalc aus (blockierend) und liefert Fingerprint + Dauer."""
    fpcalc = shutil.which("fpcalc")
    if fpcalc is None:
        _LOGGER.warning("fpcalc nicht gefunden — AcoustID-Fingerprint nicht möglich.")
        return None
    try:
        proc = subprocess.run(  # noqa: S603  -- Pfad kommt aus shutil.which, kein untrusted Input
            [fpcalc, "-json", str(path)],
            capture_output=True,
            timeout=30,
        )
    except Exception as exc:
        _LOGGER.warning("fpcalc fehlgeschlagen für %s: %s", path.name, exc)
        return None
    if proc.returncode:
        _LOGGER.warning("fpcalc konnte %s nicht dekodieren (exit %d)", path.name, proc.returncode)
        return None
    try:
        data = json.loads(proc.stdout.decode())
        if not isinstance(data, dict):
            raise ValueError("fpcalc-Ausgabe ist kein Objekt")
        return data
    except Exception as exc:
        _LOGGER.warning("fpcalc-Ausgabe nicht lesbar für %s: %s", path.name, exc)
        return None


async def acoustid_lookup(
    path: Path,
    *,
    api_key: str,
    min_score: float,
) -> tuple[AcoustidMatch | None, str]:
    """Fragt AcoustID ab.

    Gibt ``(match, status)`` zurück:
    - ``status == "ok"``      — API antwortete, ``match`` ist der beste Treffer
      (oder ``None``, wenn keiner den Mindest-Score erreicht).
    - ``status == "error"``   — API nicht erreichbar / ungültiger Key / fpcalc
      fehlgeschlagen. ``match`` ist ``None``. Die Datei soll NICHT gelöscht werden.
    """
    fp = _fpcalc_sync(path)
    if fp is None:
        return None, "error"

    # Genau ein Request mit der tatsächlich gemessenen Dauer. Ein
    # Dauer-Toleranz-Loop (mehrere duration-Werte) wäre bei vielen Sendern
    # reine API-Verschwendung — AcoustID matcht Stream-Aufnahmen ohnehin selten.
    duration = int(float(fp.get("duration", 0)))

    best: AcoustidMatch | None = None
    api_answered = False

    params = {
        "client": api_key,
        "format": "json",
        "meta": "recordings+releasegroups",
        "duration": duration,
        "fingerprint": fp.get("fingerprint", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_ACOUSTID_API_URL, params=params)
            resp.raise_for_status()
            api = resp.json()
    except Exception as exc:
        _LOGGER.warning("AcoustID-API-Fehler für %s (duration=%d): %s", path.name, duration, exc)
        return None, "error"
    if not isinstance(api, dict) or api.get("status") != "ok":
        _LOGGER.warning("AcoustID unerwartete Antwort für %s (duration=%d)", path.name, duration)
        return None, "error"
    api_answered = True

    for result in api.get("results") or []:
        try:
            score = float(result.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        for recording in result.get("recordings") or []:
            raw_artists = recording.get("artists") or []
            artists = [a.get("name", "").strip() for a in raw_artists if a.get("name")]
            artist = ", ".join(artists)
            artist_id = ""
            for a in raw_artists:
                if a.get("id"):
                    artist_id = str(a.get("id", "")).strip()
                    break
            title = (recording.get("title") or "").strip()
            if not artist or not title:
                continue
            recording_id = (recording.get("id") or "").strip()
            # Bevorzugt das erste Releasegroup als Album (single → ohne Album)
            album = ""
            year: int | None = None
            releasegroup_id = ""
            for rg in recording.get("releasegroups") or []:
                rg_title = (rg.get("title") or "").strip()
                if not album and rg_title:
                    album = rg_title
                if not releasegroup_id:
                    releasegroup_id = (rg.get("id") or "").strip()
                if year is None:
                    date = (rg.get("firstreleasedate") or "").strip()
                    if date[:4].isdigit():
                        year = int(date[:4])
            try:
                confirmations = int(recording.get("confirmations", 0) or 0)
            except (TypeError, ValueError):
                confirmations = 0
            try:
                track_number = int(recording.get("track_number") or 0) or None
            except (TypeError, ValueError):
                track_number = None

            match = AcoustidMatch(
                artist=artist,
                title=title,
                album=album,
                track_number=track_number,
                year=year,
                score=score,
                confirmations=confirmations,
                recording_id=recording_id,
                releasegroup_id=releasegroup_id,
                artist_id=artist_id,
            )
            # Besserer Score gewinnt; bei Gleichstand mehr Bestätigungen
            if best is None or (score, confirmations) > (best.score, best.confirmations):
                best = match

    if best is None:
        if not api_answered:
            _LOGGER.warning("AcoustID: keine gültige API-Antwort für %s — Fehler", path.name)
            return None, "error"
        _LOGGER.info("AcoustID: kein Treffer ≥ %.2f für %s", min_score, path.name)
    return best, "ok"


# ---------------------------------------------------------------------------
# MusicBrainz-Anreicherung (Genre, Cover, Artist-Bild)
# ---------------------------------------------------------------------------


class MusicBrainzEnrichment:
    """Holt zusätzliche Metadaten aus MusicBrainz / Cover-Art-Archive / LRCLib.

    - ``genres``        — Genre-Tags des Release-Groups
    - ``cover_data``    — Front-Cover als Bild-Bytes (500px, JPEG)
    - ``artist_image``  — Artist-Foto als Bild-Bytes (Wikimedia, wenn verfügbar)
    - ``lyrics``        — Plain-Text-Lyrics (USLT)
    - ``synced_lyrics`` — Synced-LRC-Lyrics mit Zeitstempeln (SYLT, Karaoke)
    """

    __slots__ = ("artist_image", "cover_data", "genres", "lyrics", "synced_lyrics")

    def __init__(
        self,
        genres: list[str],
        cover_data: bytes | None,
        artist_image: bytes | None,
        lyrics: str = "",
        synced_lyrics: str = "",
    ) -> None:
        self.genres = genres
        self.cover_data = cover_data
        self.artist_image = artist_image
        self.lyrics = lyrics
        self.synced_lyrics = synced_lyrics


async def _fetch_lyrics(client: httpx.AsyncClient, artist: str, title: str) -> tuple[str, str]:
    """Holt Lyrics von LRCLib.

    Liefert ``(plain_text, synced_lrc)``. Beide können leer sein, wenn kein
    Treffer gefunden wird. LRCLib liefert synced-LRC (Karaoke) und Plaintext.
    """
    if not artist or not title:
        return "", ""
    try:
        resp = await client.get(
            "https://lrclib.net/api/search",
            params={"artist_name": artist, "track_name": title},
            headers={"User-Agent": _MB_UA},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            first = data[0]
            return (
                first.get("plainLyrics") or "",
                first.get("syncedLyrics") or "",
            )
    except Exception as exc:
        _LOGGER.warning("Lyrics-Fehler für %s - %s: %s", artist, title, exc)
    return "", ""


async def _mb_get(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> Any:
    """GET gegen MusicBrainz mit korrektem User-Agent. Fehler → None."""
    try:
        query = dict(params or {})
        query.setdefault("fmt", "json")
        resp = await client.get(url, params=query, headers={"User-Agent": _MB_UA})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _LOGGER.warning("MusicBrainz-Fehler %s: %s", url, exc)
        return None


async def _fetch_genres(client: httpx.AsyncClient, releasegroup_id: str) -> list[str]:
    """Holt Genre-Tags des Release-Groups aus MusicBrainz."""
    if not releasegroup_id:
        return []
    data = await _mb_get(client, f"{_MUSICBRAINZ_API}/release-group/{releasegroup_id}", {"inc": "genres"})
    if not isinstance(data, dict):
        return []
    genres = [g.get("name", "") for g in data.get("genres") or []]
    return [g for g in genres if g]


async def _fetch_cover_art(client: httpx.AsyncClient, releasegroup_id: str) -> bytes | None:
    """Holt das Front-Cover (500px) des Release-Groups aus Cover-Art-Archive."""
    if not releasegroup_id:
        return None
    try:
        resp = await client.get(f"{_COVERART_API}/release-group/{releasegroup_id}", headers={"User-Agent": _MB_UA})
        resp.raise_for_status()
        data = resp.json()
        for image in data.get("images") or []:
            if image.get("front"):
                url = (image.get("thumbnails") or {}).get("500")
                if url:
                    img = await client.get(url, headers={"User-Agent": _MB_UA})
                    img.raise_for_status()
                    return img.content
    except Exception as exc:
        _LOGGER.warning("Cover-Art-Fehler für %s: %s", releasegroup_id, exc)
    return None


async def _fetch_artist_image(client: httpx.AsyncClient, artist_id: str) -> bytes | None:
    """Holt das Artist-Foto via MusicBrainz-Relations → Wikimedia-Commons.

    MusicBrainz liefert für Künstler eine ``image``-Relation zu Wikimedia
    Commons. Daraus wird das Bild (300px-Thumbnail) geholt.
    """
    if not artist_id:
        return None
    data = await _mb_get(client, f"{_MUSICBRAINZ_API}/artist/{artist_id}", {"inc": "url-rels"})
    if not isinstance(data, dict):
        return None
    commons_url = ""
    for rel in data.get("relations") or []:
        if rel.get("type") == "image":
            resource = (rel.get("url") or {}).get("resource") or ""
            if "commons.wikimedia.org" in resource.lower():
                commons_url = resource
                break
    if not commons_url:
        return None
    try:
        page_title = commons_url.split("/wiki/")[-1].replace("_", " ")
        params = {
            "action": "query",
            "titles": page_title,
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": "300",
            "format": "json",
        }
        resp = await client.get(
            "https://commons.wikimedia.org/w/api.php", params=params, headers={"User-Agent": _MB_UA}
        )
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            thumb = (page.get("imageinfo") or [{}])[0].get("thumburl")
            if thumb:
                img = await client.get(thumb, headers={"User-Agent": _MB_UA})
                img.raise_for_status()
                return img.content
    except Exception as exc:
        _LOGGER.warning("Artist-Bild-Fehler für %s: %s", artist_id, exc)
    return None


async def enrich_musicbrainz(match: AcoustidMatch, client: httpx.AsyncClient) -> MusicBrainzEnrichment:
    """Holt Genre, Cover, Artist-Bild und Lyrics für einen AcoustID-Treffer (parallel)."""
    genres_task = asyncio.create_task(_fetch_genres(client, match.releasegroup_id))
    cover_task = asyncio.create_task(_fetch_cover_art(client, match.releasegroup_id))
    artist_task = asyncio.create_task(_fetch_artist_image(client, match.artist_id))
    lyrics_task = asyncio.create_task(_fetch_lyrics(client, match.artist, match.title))
    genres, cover, artist, (lyrics, synced) = await asyncio.gather(genres_task, cover_task, artist_task, lyrics_task)
    return MusicBrainzEnrichment(
        genres=genres,
        cover_data=cover,
        artist_image=artist,
        lyrics=lyrics,
        synced_lyrics=synced,
    )


def _add_synced_lyrics(audio: Any, lrc_text: str) -> None:
    """Schreibt synced-LRC-Lyrics als SYLT-Frames (Karaoke).

    LRC-Format: ``[mm:ss.xx] Textzeile`` → SYLT mit Zeitstempel in Millisekunden.
    """
    from mutagen.id3 import SYLT

    sylt_other = 0
    entries: list[tuple[str, int]] = []
    for line in lrc_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Sammle alle Zeitstempel am Zeilenanfang: [mm:ss.xx][mm:ss.xx]Text
        import re

        times = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        text = re.sub(r"\[(?:\d+):(?:\d+(?:\.\d+)?)\](?=\s*[^\[]|$)", "", line).strip()
        if not times or not text:
            continue
        for m, s in times:
            millis = int(m) * 60000 + round(float(s) * 1000)
            entries.append((text, millis))

    if not entries:
        return
    frame = SYLT(encoding=3, lang="eng", format=sylt_other, type=0, desc="Lyrics")  # type: ignore[no-untyped-call]
    frame.text = entries  # type: ignore[assignment]
    audio.add(frame)  # type: ignore[no-untyped-call]


def write_mp3_tags(
    path: Path,
    *,
    artist: str,
    title: str,
    album: str = "",
    track_number: int | None = None,
    year: int | None = None,
    score: float,
    confirmations: int = 0,
    recording_id: str = "",
    releasegroup_id: str = "",
    genres: list[str] | None = None,
    cover_data: bytes | None = None,
    artist_image: bytes | None = None,
    lyrics: str = "",
    synced_lyrics: str = "",
) -> None:
    """Schreibt Artist/Title/Album/Track/Jahr/Score + MB-IDs + Genre/Cover/Lyrics als ID3-Tags."""
    try:
        from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TRCK, TXXX, USLT, ID3NoHeaderError
    except ImportError:
        _LOGGER.warning("mutagen nicht installiert — Tags werden nicht geschrieben für %s", path.name)
        return
    try:
        try:
            audio = ID3(path)  # type: ignore[no-untyped-call]
        except ID3NoHeaderError:
            audio = ID3()  # type: ignore[no-untyped-call]
        if artist:
            audio.add(TPE1(encoding=3, text=[artist]))  # type: ignore[no-untyped-call]
        if title:
            audio.add(TIT2(encoding=3, text=[title]))  # type: ignore[no-untyped-call]
        if album:
            audio.add(TALB(encoding=3, text=[album]))  # type: ignore[no-untyped-call]
        if track_number:
            audio.add(TRCK(encoding=3, text=[str(track_number)]))  # type: ignore[no-untyped-call]
        if year:
            audio.add(TDRC(encoding=3, text=[str(year)]))  # type: ignore[no-untyped-call]
        if genres:
            audio.add(TCON(encoding=3, text=genres[:3]))  # type: ignore[no-untyped-call]
        audio.add(TXXX(encoding=3, desc=_ACOUSTID_SCORE_TAG, text=[f"{score:.6f}"]))  # type: ignore[no-untyped-call]
        if confirmations:
            audio.add(TXXX(encoding=3, desc="ACOUSTID_CONFIRMATIONS", text=[str(confirmations)]))  # type: ignore[no-untyped-call]
        if recording_id:
            audio.add(TXXX(encoding=3, desc="MUSICBRAINZ_TRACKID", text=[recording_id]))  # type: ignore[no-untyped-call]
        if releasegroup_id:
            audio.add(TXXX(encoding=3, desc="MUSICBRAINZ_RELEASEGROUPID", text=[releasegroup_id]))  # type: ignore[no-untyped-call]
        if cover_data:
            audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data))  # type: ignore[no-untyped-call]
        if artist_image:
            audio.add(APIC(encoding=3, mime="image/jpeg", type=8, desc="Artist", data=artist_image))  # type: ignore[no-untyped-call]
        if synced_lyrics:
            _add_synced_lyrics(audio, synced_lyrics)
        elif lyrics:
            audio.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=lyrics))  # type: ignore[no-untyped-call]
        audio.save(path)  # type: ignore[no-untyped-call]
    except Exception as exc:
        _LOGGER.warning("ID3-Tags für %s fehlgeschlagen: %s", path.name, exc)


def build_metadata_filename(artist: str, title: str) -> str:
    """Baut ``Artist - Title.mp3`` (säuberter Dateiname)."""
    raw = f"{artist} - {title}".strip(" -")
    safe = sanitize_filename(raw)
    return safe + ".mp3" if safe else ""


def build_target_path(destination: Path, artist: str, title: str, album: str = "") -> Path:
    """Baut den Zielpfad mit standardmäßiger MP3-Ordnerstruktur.

    Struktur: ``Artist/Album/Artist - Title.mp3``
    Fallback ohne Album: ``Artist/Artist - Title.mp3``
    """
    safe_artist = sanitize_filename(artist)
    if not safe_artist:
        safe_artist = "Unknown Artist"
    filename = build_metadata_filename(artist, title)
    if not filename:
        return destination / safe_artist / f"{safe_artist}.mp3"

    safe_album = sanitize_filename(album)
    if safe_album:
        return destination / safe_artist / safe_album / filename
    return destination / safe_artist / filename


def read_mp3_score(path: Path) -> float | None:
    """Liest den gespeicherten ``ACOUSTID_SCORE``-Tag einer MP3, oder None."""
    try:
        from mutagen.id3 import ID3, TXXX

        tags = ID3(path)  # type: ignore[no-untyped-call]
        for frame in tags.values():  # type: ignore[no-untyped-call]
            if isinstance(frame, TXXX) and frame.desc == _ACOUSTID_SCORE_TAG:  # type: ignore[attr-defined]
                return float(frame.text[0])  # type: ignore[attr-defined]
    except Exception:
        return None
    return None


def move_to_destination(path: Path, target: Path) -> None:
    """Verschiebt path nach target (mit Cross-Device-Fallback)."""
    try:
        os.replace(str(path), str(target))
        return
    except OSError as exc:
        if exc.errno != _EXDEV:
            raise
    shutil.copy2(path, target)
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _move_to_unmatched(final_path: Path, work_dir: Path) -> None:
    """Verschiebt eine nicht erkannte Datei nach work_dir/recordings/unmatched/."""
    unmatched_dir = work_dir / "recordings" / "unmatched"
    with contextlib.suppress(OSError):
        unmatched_dir.mkdir(parents=True, exist_ok=True)
        target = unmatched_dir / final_path.name
        if target.exists():
            target = unmatched_dir / f"{final_path.stem}.{uuid.uuid4().hex[:8]}.mp3"
        shutil.move(str(final_path), str(target))


async def finalize_acoustid(final_path: Path, settings: Settings) -> None:
    """Läuft nach der Validierung: AcoustID-Lookup, Tagging, Verschieben.

    - Treffer ≥ min_score: Tags schreiben, nach destination/ verschieben
      (Ordnerstruktur Artist/Album/), bei Kollision gewinnt der höhere Score.
    - Kein Treffer: Datei nach recordings/unmatched/ verschieben (bleibt zum
      manuellen Anhören erhalten).
    - Fehler: Datei bleibt in recordings/ liegen.
    """
    if not settings.acoustid_api_key:
        _LOGGER.info(
            "[acoustid] Kein API-Key — Datei bleibt in recordings/: %s",
            final_path.name,
        )
        return

    match, status = await acoustid_lookup(
        final_path,
        api_key=settings.acoustid_api_key,
        min_score=settings.acoustid_min_score,
    )
    if status == "error":
        _LOGGER.info(
            "[acoustid] API-Fehler — Datei bleibt in recordings/: %s",
            final_path.name,
        )
        return
    if match is None:
        _LOGGER.info("[acoustid] Kein Treffer — verschiebe nach unmatched/: %s", final_path.name)
        _move_to_unmatched(final_path, settings.work_dir)
        return

    target = build_target_path(
        settings.destination,
        match.artist,
        match.title,
        match.album,
    )

    # MusicBrainz-Anreicherung: Genre, Cover, Artist-Bild, Lyrics (fehlertolerant)
    enrichment = MusicBrainzEnrichment(genres=[], cover_data=None, artist_image=None)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            enrichment = await enrich_musicbrainz(match, client)
    except Exception as exc:
        _LOGGER.warning("[acoustid] MusicBrainz-Anreicherung fehlgeschlagen für %s: %s", final_path.name, exc)

    # Tags zuerst schreiben, damit Kollision via ACOUSTID_SCORE vergleichbar ist
    write_mp3_tags(
        final_path,
        artist=match.artist,
        title=match.title,
        album=match.album,
        track_number=match.track_number,
        year=match.year,
        score=match.score,
        confirmations=match.confirmations,
        recording_id=match.recording_id,
        releasegroup_id=match.releasegroup_id,
        genres=enrichment.genres,
        cover_data=enrichment.cover_data,
        artist_image=enrichment.artist_image,
        lyrics=enrichment.lyrics,
        synced_lyrics=enrichment.synced_lyrics,
    )

    # Kollision: bestehende Datei mit höherem/gleichem Score behalten.
    # Die Prüfung + Verschiebung ist durch _FINALIZE_LOCK geschützt, damit
    # parallele Worker nie gleichzeitig auf dasselbe Ziel zugreifen.
    with _FINALIZE_LOCK:
        if target.exists():
            existing_score = read_mp3_score(target)
            if existing_score is not None and existing_score >= match.score:
                _LOGGER.info(
                    "[acoustid] Kollision: %s behalten (Score %.2f >= %.2f) — verwerfe %s",
                    target.name,
                    existing_score,
                    match.score,
                    final_path.name,
                )
                with contextlib.suppress(OSError):
                    final_path.unlink(missing_ok=True)
                return
            if existing_score is not None:
                _LOGGER.info(
                    "[acoustid] Kollision: ersetze %s (Score %.2f < %.2f)",
                    target.name,
                    existing_score,
                    match.score,
                )
            else:
                _LOGGER.info(
                    "[acoustid] Kollision: bestehende %s ohne Score — ersetze (Score %.2f)",
                    target.name,
                    match.score,
                )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            move_to_destination(final_path, target)
        except OSError as exc:
            _LOGGER.error("[acoustid] Verschieben fehlgeschlagen für %s: %s", final_path.name, exc)
            return

    _LOGGER.info(
        "[acoustid] Accepted: %s (score=%.2f)",
        target,
        match.score,
    )


class AcoustidWorker:
    """Singleton-Worker für AcoustID — verarbeitet fertige Aufnahmen sequenziell.

    Recorder legen fertige, validierte Dateien per ``enqueue()`` in eine Queue
    und streamen sofort weiter. Ein einzelner asyncio-Task verarbeitet die
    Dateien nacheinander (fpcalc + API + Tagging + Verschieben), was zugleich
    ein natürliches Rate-Limit für die AcoustID-API darstellt. Die blockierenden
    Aufrufe laufen via ``asyncio.to_thread`` im Default-Executor — getrennt vom
    ffprobe-ThreadPool.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def enqueue(self, path: Path) -> None:
        """Reicht eine fertige Datei zur Verarbeitung ein (blockiert nie)."""
        if self._stopped:
            return
        self._queue.put_nowait(path)

    def start(self) -> None:
        """Startet den Worker-Task (muss in einer laufenden Event-Loop sein)."""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="AcoustidWorker")

    async def stop(self) -> None:
        """Stoppt den Worker nach dem Abarbeiten der Reste der Queue."""
        self._stopped = True
        self._queue.put_nowait(_STOP_SENTINEL)
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=30.0)
            self._task = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def _run(self) -> None:
        while True:
            path = await self._queue.get()
            if path.name == _STOP_SENTINEL.name:
                break
            try:
                await finalize_acoustid(path, self._settings)
            except Exception:
                _LOGGER.exception("AcoustidWorker: unerwarteter Fehler für %s", path.name)
