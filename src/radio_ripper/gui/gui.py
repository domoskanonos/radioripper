"""Gradio GUI for Radio-Ripper — thin presentation layer.

All business logic is delegated to :mod:`radio_ripper.api`.
This module only builds Gradio components and wires callbacks.

Tabs:
    1. **Sender**       — manage radio stations (add/edit/remove)
    2. **Konfiguration** — edit all ripper settings
    3. **Bibliothek**    — browse, search, play, and delete recorded songs
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from radio_ripper.api import ConfigApi, LibraryApi, RipperApi, StationApi
from radio_ripper.infra.config import Settings, load_settings
from radio_ripper.infra.errors import ConfigurationError

if TYPE_CHECKING:
    import gradio as gr

_LOGGER = logging.getLogger(__name__)

__all__ = ["build_app", "main"]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _settings_to_dataframe_rows(settings: Settings) -> list[list[str]]:
    """Convert ``settings.streams`` to Gradio DataFrame rows."""
    return [[s.name, str(s.url)] for s in settings.streams]


def _settings_to_config_dict(settings: Settings) -> dict[str, Any]:
    """Flatten ``Settings`` into a dict for config form initialisation."""
    return settings.model_dump(mode="json")


# ──────────────────────────────────────────────────────────────────────
# App builder
# ──────────────────────────────────────────────────────────────────────


def build_app(config_path: str | Path) -> gr.Blocks:
    """Build and return the Gradio ``Blocks`` application.

    Args:
        config_path: Path to the ``config.json`` file.
    """
    import gradio as gr

    config_api = ConfigApi(config_path)
    try:
        config_api.load()
    except ConfigurationError:
        config_api._settings = ConfigApi.default_settings()

    station_api = StationApi(config_api)
    ripper_api = RipperApi()
    settings = config_api.settings()

    with gr.Blocks(
        title="Radio-Ripper GUI",
        theme=gr.themes.Soft(),
        css="footer {visibility: hidden}",
    ) as app:
        gr.Markdown("# 📻 Radio-Ripper GUI")

        # ── Tab 1: Sender ─────────────────────────────────────────────
        with gr.Tab("Sender"):
            stations_df = gr.Dataframe(
                headers=["Name", "URL"],
                datatype=["str", "str"],
                value=_settings_to_dataframe_rows(settings),
                interactive=False,
                wrap=True,
            )

            with gr.Accordion("Sender hinzufügen", open=True):
                new_name = gr.Textbox(label="Name", placeholder="z.B. TopHits")
                new_url = gr.Textbox(label="URL", placeholder="http://…/listen.m3u")
                add_btn = gr.Button("+ Hinzufügen", variant="primary")
                add_status = gr.Textbox(label="Status", interactive=False)

            with gr.Accordion("Sender bearbeiten", open=False):
                edit_old_name = gr.Textbox(label="Aktueller Name")
                edit_new_name = gr.Textbox(label="Neuer Name")
                edit_new_url = gr.Textbox(label="Neue URL")
                edit_btn = gr.Button("Speichern")
                edit_status = gr.Textbox(label="Status", interactive=False)

            with gr.Accordion("Sender entfernen", open=False):
                del_name = gr.Textbox(label="Name des zu löschenden Senders")
                del_btn = gr.Button("Entfernen", variant="stop")
                del_status = gr.Textbox(label="Status", interactive=False)

            def cb_add(name: str, url: str) -> tuple[list[list[str]], str]:
                try:
                    new_settings = station_api.add_station(name, url)
                    config_api.save(new_settings)
                    return _settings_to_dataframe_rows(new_settings), f"✅ '{name}' hinzugefügt."
                except ConfigurationError as exc:
                    return _settings_to_dataframe_rows(config_api.settings()), f"❌ {exc}"

            add_btn.click(
                cb_add,
                inputs=[new_name, new_url],
                outputs=[stations_df, add_status],
            )

            def cb_edit_old(
                old_name: str, new_name: str, new_url: str
            ) -> tuple[list[list[str]], str]:
                try:
                    new_settings = station_api.edit_station(old_name, new_name, new_url)
                    config_api.save(new_settings)
                    return _settings_to_dataframe_rows(
                        new_settings
                    ), f"✅ '{old_name}' → '{new_name}'."
                except ConfigurationError as exc:
                    return _settings_to_dataframe_rows(config_api.settings()), f"❌ {exc}"

            edit_btn.click(
                cb_edit_old,
                inputs=[edit_old_name, edit_new_name, edit_new_url],
                outputs=[stations_df, edit_status],
            )

            def cb_del(name: str) -> tuple[list[list[str]], str]:
                try:
                    new_settings = station_api.remove_station(name)
                    config_api.save(new_settings)
                    return _settings_to_dataframe_rows(new_settings), f"✅ '{name}' entfernt."
                except ConfigurationError as exc:
                    return _settings_to_dataframe_rows(config_api.settings()), f"❌ {exc}"

            del_btn.click(
                cb_del,
                inputs=[del_name],
                outputs=[stations_df, del_status],
            )

        # ── Tab 2: Konfiguration ─────────────────────────────────────
        with gr.Tab("Konfiguration"):
            cfg_data = _settings_to_config_dict(settings)

            cfg_destination = gr.Textbox(
                label="Destination", value=str(cfg_data.get("destination", "./recordings"))
            )
            cfg_database = gr.Textbox(
                label="Datenbank-Pfad",
                value=str(cfg_data.get("database", "./recordings/ripper.db")),
            )
            cfg_request_timeout = gr.Number(
                label="Request Timeout (s)", value=cfg_data.get("request_timeout", 30)
            )
            cfg_read_chunk = gr.Number(
                label="Read Chunk (bytes)", value=cfg_data.get("read_chunk", 4096)
            )
            cfg_reconnect_base = gr.Number(
                label="Reconnect Base Delay (s)", value=cfg_data.get("reconnect_base_delay", 1.0)
            )
            cfg_reconnect_max = gr.Number(
                label="Reconnect Max Delay (s)", value=cfg_data.get("reconnect_max_delay", 60.0)
            )
            cfg_user_agent = gr.Textbox(
                label="User Agent", value=cfg_data.get("user_agent", "Radio-Ripper/2.0")
            )
            cfg_overwrite = gr.Checkbox(
                label="Existing Files Overwrite",
                value=cfg_data.get("overwrite_existing_files", False),
            )
            cfg_min_size = gr.Number(
                label="Min File Size (bytes)", value=cfg_data.get("min_file_size_bytes", 1024)
            )
            cfg_log_level = gr.Dropdown(
                label="Log Level",
                choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                value=cfg_data.get("log_level", "INFO"),
            )
            cfg_log_file = gr.Textbox(label="Log File", value=str(cfg_data.get("log_file", "")))

            with gr.Row():
                cfg_enrich = gr.Checkbox(
                    label="iTunes Enrichment", value=cfg_data.get("enrich_metadata", True)
                )
                cfg_cover = gr.Checkbox(
                    label="Cover Art embed", value=cfg_data.get("embed_cover_art", True)
                )
                cfg_workers = gr.Number(
                    label="Enrichment Workers", value=cfg_data.get("enrichment_workers", 4)
                )
            cfg_metadata_timeout = gr.Number(
                label="Metadata Timeout (s)", value=cfg_data.get("metadata_timeout", 8.0)
            )
            cfg_cover_timeout = gr.Number(
                label="Cover Timeout (s)", value=cfg_data.get("cover_timeout", 15.0)
            )

            save_btn = gr.Button("Konfiguration speichern", variant="primary")
            cfg_status = gr.Textbox(label="Status", interactive=False)

            def cb_save_config(
                destination: str,
                database: str,
                request_timeout: float,
                read_chunk: int,
                reconnect_base: float,
                reconnect_max: float,
                user_agent: str,
                overwrite: bool,
                min_size: int,
                log_level: str,
                log_file: str,
                enrich: bool,
                cover: bool,
                workers: int,
                metadata_timeout: float,
                cover_timeout: float,
            ) -> str:
                try:
                    current = config_api.settings()
                    new_settings = current.model_copy(
                        update={
                            "destination": Path(destination),
                            "database": Path(database),
                            "request_timeout": request_timeout,
                            "read_chunk": read_chunk,
                            "reconnect_base_delay": reconnect_base,
                            "reconnect_max_delay": reconnect_max,
                            "user_agent": user_agent,
                            "overwrite_existing_files": overwrite,
                            "min_file_size_bytes": min_size,
                            "log_level": log_level,
                            "log_file": Path(log_file) if log_file else None,
                            "enrich_metadata": enrich,
                            "embed_cover_art": cover,
                            "enrichment_workers": workers,
                            "metadata_timeout": metadata_timeout,
                            "cover_timeout": cover_timeout,
                        }
                    )
                    config_api.save(new_settings)
                    return "✅ Konfiguration gespeichert."
                except (ConfigurationError, ValueError) as exc:
                    return f"❌ {exc}"
                except Exception as exc:
                    return f"❌ Unerwarteter Fehler: {exc}"

            save_btn.click(
                cb_save_config,
                inputs=[
                    cfg_destination,
                    cfg_database,
                    cfg_request_timeout,
                    cfg_read_chunk,
                    cfg_reconnect_base,
                    cfg_reconnect_max,
                    cfg_user_agent,
                    cfg_overwrite,
                    cfg_min_size,
                    cfg_log_level,
                    cfg_log_file,
                    cfg_enrich,
                    cfg_cover,
                    cfg_workers,
                    cfg_metadata_timeout,
                    cfg_cover_timeout,
                ],
                outputs=[cfg_status],
            )

        # ── Tab 3: Bibliothek ────────────────────────────────────────
        with gr.Tab("Bibliothek"):
            library_api = LibraryApi(settings)

            search_box = gr.Textbox(
                label="Suche (Artist, Titel, Sender)", placeholder="Adele Hello…"
            )
            search_btn = gr.Button("Suchen")

            _initial_songs = [
                [
                    s.id,
                    s.station_name,
                    s.stream_title,
                    s.artist,
                    s.title,
                    s.album or "",
                    s.year or "",
                    round(s.file_size / 1024, 1),
                    s.has_cover,
                    s.created_at,
                ]
                for s in library_api.list_songs()
            ] or [[]]

            songs_df = gr.Dataframe(
                headers=[
                    "ID",
                    "Sender",
                    "StreamTitle",
                    "Artist",
                    "Titel",
                    "Album",
                    "Jahr",
                    "Größe (KB)",
                    "Cover",
                    "Aufgenommen",
                ],
                datatype=[
                    "number",
                    "str",
                    "str",
                    "str",
                    "str",
                    "str",
                    "str",
                    "number",
                    "bool",
                    "str",
                ],
                value=_initial_songs,
                interactive=False,
                wrap=True,
            )

            selected_id = gr.Number(label="Song ID zum Abspielen/Löschen")
            audio_player = gr.Audio(label="Player", interactive=False)
            action_btn_row = gr.Row()
            with action_btn_row:
                play_btn = gr.Button("Abspielen")
                del_btn2 = gr.Button("Löschen", variant="stop")
            lib_status = gr.Textbox(label="Status", interactive=False)

            def cb_search(query: str) -> list[list[Any]]:
                lib = LibraryApi(config_api.settings())
                songs = lib.search_songs(query) if query.strip() else lib.list_songs()
                return [
                    [
                        s.id,
                        s.station_name,
                        s.stream_title,
                        s.artist,
                        s.title,
                        s.album or "",
                        s.year or "",
                        round(s.file_size / 1024, 1),
                        s.has_cover,
                        s.created_at,
                    ]
                    for s in songs
                ]

            search_btn.click(cb_search, inputs=[search_box], outputs=[songs_df])

            def cb_play(song_id: float) -> tuple[Any, str]:
                lib = LibraryApi(config_api.settings())
                song = lib.get_song(int(song_id))
                if song is None:
                    return None, "❌ Song nicht gefunden."
                if song.absolute_path is None:
                    return None, "❌ Datei existiert nicht auf der Festplatte."
                return song.absolute_path, f"▶️ Spiele: {song.artist} - {song.title}"

            play_btn.click(cb_play, inputs=[selected_id], outputs=[audio_player, lib_status])

            def cb_delete_song(song_id: float) -> tuple[list[list[Any]], str]:
                lib = LibraryApi(config_api.settings())
                ok = lib.delete_song(int(song_id))
                if not ok:
                    songs = cb_search("")
                    return songs, f"❌ Song ID {int(song_id)} nicht gefunden."
                songs = cb_search("")
                return songs, f"✅ Song ID {int(song_id)} gelöscht."

            del_btn2.click(cb_delete_song, inputs=[selected_id], outputs=[songs_df, lib_status])

        # ── Tab 4: Ripper-Steuerung ───────────────────────────────────
        with gr.Tab("Ripper"):
            gr.Markdown("### Ripper starten / stoppen")
            ripper_state = gr.Textbox(
                label="Status",
                value=f"Status: {ripper_api.status.value}",
                interactive=False,
            )
            with gr.Row():
                start_btn = gr.Button("Ripper starten", variant="primary")
                stop_btn = gr.Button("Ripper stoppen", variant="stop")

            def cb_start_ripper() -> str:
                try:
                    current_settings = config_api.reload()
                    msg = ripper_api.start(current_settings)
                    return f"Status: {ripper_api.status.value} — {msg}"
                except ConfigurationError as exc:
                    return f"❌ {exc}"

            start_btn.click(cb_start_ripper, outputs=[ripper_state])

            def cb_stop_ripper() -> str:
                msg = ripper_api.stop()
                return f"Status: {ripper_api.status.value} — {msg}"

            stop_btn.click(cb_stop_ripper, outputs=[ripper_state])

    return app


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the Gradio GUI.  Entry point for ``radio-ripper-gui``."""
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        config_path = "./config.json"
    elif args[0] == "--config" and len(args) >= 2:
        config_path = args[1]
    elif args[0] == "--config":
        print("Fehler: --config erwartet einen Pfad", file=sys.stderr)
        return 2
    elif args[0].startswith("--"):
        print(f"Unbekannte Option: {args[0]}", file=sys.stderr)
        return 2
    else:
        config_path = args[0]

    if not Path(config_path).is_file():
        print(f"Config nicht gefunden: {config_path}", file=sys.stderr)
        return 2

    try:
        load_settings(config_path)
    except ConfigurationError as exc:
        print(f"Config-Fehler: {exc}", file=sys.stderr)
        return 2

    app = build_app(config_path)
    app.launch(server_name="0.0.0.0", server_port=7860, show_error=True)
    return 0
