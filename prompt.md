* Basisverzeichnis für die Ablage ist der konfigurierbare Unterordner `Radio-Aufnahmen`.
* Für jeden Künstler wird ein eigener Unterordner erstellt.
* Innerhalb des Künstler-Ordners wird für jedes Album ein eigener Unterordner erstellt.
* Wenn kein Albumname vom Stream übergeben wird, wird der Ordnername `"Radio-Aufnahmen"` (oder der Sendername) als Fallback-Albumname genutzt.
* Dateipfad-Struktur folgt dem Schema: `Radio-Aufnahmen/[artist]/[album]/[artist] - [Songtitle].mp3`
* ID3-Metatags werden im Format ID3v2.3 oder ID3v2.4 geschrieben.
* Der Tag **Title** (TIT2) wird mit `[Songtitle]` belegt.
* Der Tag **Artist** (TPE1) wird mit `[artist]` belegt.
* Der Tag **Album Artist** (TPE2) wird zwingend identisch zu `[artist]` gesetzt.
* Der Tag **Album** (TALB) wird mit `[album]` belegt (Fallback bei fehlendem Wert: `"Radio-Aufnahmen"` oder Sendername).
* Das Cover-Art wird direkt als ID3-Tag (APIC, Bildtyp 03 / Front Cover) in die MP3-Datei eingebettet.
* Bildformate für das Cover sind auf `image/jpeg` oder `image/png` beschränkt.
* Die Cover-Auflösung wird vor dem Einbetten auf eine Zielgröße zwischen 500x500 und maximal 1000x1000 Pixel skaliert.
* Bei fehlendem Stream-Cover wird ein definiertes Standard-Fallback-Bild (z. B. Sender-Logo) eingebettet.
* Fehlerhafte Audio-Frame-Header am Anfang und Ende des geschnittenen Streams werden repariert, um eine korrekte Track-Länge und fehlerfreies Spulen sicherzustellen.