# ADR-0002: Service-ABCs fur Dependency Injection

**Status:** Angenommen

**Kontext:** Services wie TrackRepository, TrackTagger und MetadataProvider sollen austauschbar sein (z. B. SQLite vs. PostgreSQL, ID3 vs. Vorbis).

**Entscheidung:** Jeder Service definiert ein `abc.ABC` mit abstrakten Methoden. Die Implementierung wird per Constructor-Injection in den Service gegeben.

**Konsequenzen:** Tests konnen leicht Mock-Implementierungen einspritzen. Neue Backends konnen ohne Anderung der Business-Logik hinzugefugt werden.
