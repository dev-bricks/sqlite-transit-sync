# Decisions

## ADR-001: Snapshot-Transit statt gemeinsam geöffneter Datenbank

Jeder Knoten besitzt eine lokale SQLite-Datenbank. Nur geschlossene, geprüfte
Snapshots werden transportiert. Das erhält SQLite-Lokalität und vermeidet WAL über
Netzwerk- oder Cloud-Dateisysteme.

## ADR-002: Anwendungsschema muss vor Pull existieren

Das Modul kopiert keinen fremden Snapshot als erste lokale Datenbank. Schema,
Migrationen, lokale Tabellen und Geheimnisse sind fachliche Entscheidungen der
Anwendung und dürfen nicht generisch geraten werden.

## ADR-003: Policy-Schnittstelle statt universeller Konfliktbehauptung

Timestamp-LWW ist nur die sichere Baseline. Löschungen, Tombstones, CRDTs,
Zeitabweichungen und fachliche Validierung gehören in eine anwendungsspezifische
`MergePolicy`.

## ADR-004: Integrität ist nicht Authentizität

SHA-256 und Manifest erkennen Übertragungsfehler. Ein feindlicher Transport benötigt
zusätzlich Signaturen oder einen authentifizierten Kanal.

