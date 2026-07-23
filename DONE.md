# DONE

## 2026-07-13

- [x] Snapshot-Redaktion härtet veröffentlichte Dateien mit `VACUUM` nach dem Löschen ausgeschlossener Tabellen.
- [x] Regressionstest ergänzt, der prüft, dass synthetische Secret-Bytes nicht im publizierten Snapshot verbleiben.

## 2026-07-11

- [x] BACH-ProSync-Mechanismus als nutzerneutrales Standalone-Modul extrahiert.
- [x] Feste BACH-/OneDrive-/Benutzerpfade durch `SyncConfig` ersetzt.
- [x] Python-API, JSON-CLI, Snapshot-Manifeste und lokale Zustandsführung implementiert.
- [x] Anpassbare Merge-Policy und sichere Standardpolicy implementiert.
- [x] Sicherheits-, Architektur- und Endnutzerdokumentation angelegt.
- [x] Synthetischen Cross-Node-Roundtrip einschließlich Manipulationsschutz getestet.
