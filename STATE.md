# STATE.md

**Stand:** 2026-07-11  
**Phase:** Alpha / neutrale Extraktion abgeschlossen

## Funktionsfähig

- Republica-Modus: verschlüsselte Einweg-Verteilung (`publish`/`republica-import`),
  separate schreibgeschützte Replica je Quellknoten, kein Merge (ADR-006 bis ADR-008)
- kuratierter SQL-Dump mit korrektem Wiederherstellen von FTS-Volltextindizes

- Konfigurierbare Python-API und JSON-CLI
- geprüfte SQLite-Snapshots mit Manifest und SHA-256
- lokaler Pull-State je Knoten
- Primärschlüssel-basierter Timestamp-Merge
- Schema-Drift über gemeinsame Spalten
- Merge-Ausschlüsse und Snapshot-Redaktion
- Credential-Scan des Snapshot-Inhalts vor Veröffentlichung (fail-closed, ADR-005)
- eigenständige synthetische Tests

## Noch nicht integriert

- BACH nutzt weiterhin seine bewährte interne ProSync-Implementierung.
- Es gibt noch keinen Signatur-/Authentifizierungsadapter. Fernet im Replica-Modus
  authentifiziert den Schlüssel, nicht den Absender — Knoten-Identität bleibt offen.
- Retention und Tombstones bleiben anwendungsspezifisch.
- Replica-Snapshots werden im Transit nicht automatisch aufgeräumt (siehe Retention).

## Letzte Dokumentationsänderung

- 2026-07-11: `README.md` und `README_de.md` um den vollständigen Vergleich mit
  Distributed SQL, Vor-/Nachteile, Use Cases und Entscheidungshilfe ergänzt.
