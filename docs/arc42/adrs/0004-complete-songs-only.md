# ADR-0004: Nur komplette Songs speichern

**Status:** Angenommen

**Kontext:** Bei Verbindungsabbruchen konnte ein Song fragmentiert auf der Festplatte landen.

**Entscheidung:** Der `TrackWriter` schreibt zunachst in eine temporare Datei. Erst bei erfolgreichem Titel-Wechsel (neuer `StreamTitle`) wird die Datei atomar via `os.rename` ins Zielverzeichnis verschoben. Bei Shutdown oder Fehler wird die Temp-Datei verworfen.

**Konsequenzen:** Keine halben Songs. Minimale Latenz zwischen Aufnahme-Ende und Sichtbarkeit im Zielordner (atomic rename ist instantan).
