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

## ADR-005: Der Credential-Scan bricht ab, statt zu löschen

`snapshot_exclude_tables` setzt voraus, dass man die betroffene Tabelle bereits kennt.
Das deckt den geplanten Fall ab, nicht den häufigeren: ein Zugangsdatum, das jemand in
ein Freitextfeld geschrieben hat — eine Notiz, eine Logzeile, eine Sitzungszusammenfassung.
Deshalb prüft `scan_snapshot_for_secrets` (standardmäßig aktiv) den Snapshot-Inhalt, bevor
er das Transitverzeichnis erreicht.

**Der Fund führt zum Abbruch, nicht zur automatischen Bereinigung.** Das folgt ADR-002:
Welche Daten geheim sind, ist eine fachliche Entscheidung der Anwendung, die dieses Modul
nicht raten darf. Es darf aber die Veröffentlichung verweigern und den Fundort melden —
Anhalten ist die einzige Reaktion, die keine fremde Entscheidung vorwegnimmt. Stilles
Löschen würde zudem verschleiern, dass ein Geheimnis überhaupt in der Quelldatenbank liegt;
das Problem gehört dort behoben, nicht im Snapshot kaschiert.

**Die Muster sind bewusst herstellerpräfixiert** (`sk-`, `ghp_`, `AKIA`, `-----BEGIN … PRIVATE KEY-----`
und weitere). Eine generische Regel wie „langer Hex-String" würde Prüfsummen, UUIDs und
Content-Hashes markieren, die in Datenbanken völlig legitim sind. Ein Scanner mit hoher
Fehlalarmrate wird abgeschaltet — und ein abgeschalteter Scanner schützt nichts.

**Fehlermeldungen nennen nie den gefundenen Wert**, nur `tabelle.spalte`. Andernfalls
wanderte das Geheimnis in Logs, Tracebacks und CI-Ausgaben, also genau dorthin, wovor
die Prüfung schützen soll.

## ADR-004: Integrität ist nicht Authentizität

SHA-256 und Manifest erkennen Übertragungsfehler. Ein feindlicher Transport benötigt
zusätzlich Signaturen oder einen authentifizierten Kanal.

