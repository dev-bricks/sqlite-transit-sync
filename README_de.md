# sqlite-transit-sync

`sqlite-transit-sync` gleicht unabhängige lokale SQLite-Datenbanken über geprüfte
Snapshots und anpassbare Merge-Regeln ab. Das Modul wurde aus der
BACH-ProSync-Architektur extrahiert und enthält keine BACH-, OneDrive-, Rechner-
oder Benutzerpfade.

Es ist **kein verteilter SQL-Server**: Jeder Knoten öffnet ausschließlich seine
lokale Datenbank. Ein gemeinsamer Transportordner trägt geschlossene Snapshots
mit Manifesten. Beim Pull werden Integrität und Prüfsumme kontrolliert und die
Daten anschließend in einer lokalen Transaktion zusammengeführt.

Passt gut zu [sync-master](https://github.com/dev-bricks/sync-master)
(gleiche Modul-Familie): Ein sync-master-Yard ist ein natürlicher
Transit-Transport — `--transit` auf eine tool-eigene Zone
`db-transit/<namespace>/` im Yard zeigen lassen (dort Protokoll-Regel R9).
sync-master trägt die Dokumente, dieses Modul verantwortet
Datenbank-Integrität und Merge; beide bleiben unabhängig.

## Teil der ellmos-Stack-Familie

`sqlite-transit-sync` ist ein Companion-Modul zu
[dev-bricks/sync-master](https://github.com/dev-bricks/sync-master) aus
derselben Modul-Familie: sync-master verantwortet den Dateitransport
(`sync.files`), dieses Modul die Datenbank-Integrität und den Merge
(`sync.database`). Beide sind eigenständig nutzbar oder in Stacks der
[ellmos-ai](https://github.com/ellmos-ai)-Familie kombinierbar — siehe den
Stack-Katalog [ellmos-ai/stacks](https://github.com/ellmos-ai/stacks). Seine
Baukasten-Rolle: kein Live-SQLite über Datei-Sync — nur geprüfte Snapshots
mit anwendungsspezifischen Merge-Policies.

## Eigenschaften

- konsistente Snapshots über die SQLite-Backup-API;
- atomare Veröffentlichung und SHA-256-Manifest;
- `PRAGMA quick_check` vor Verarbeitung;
- lokaler Pull-Zustand je Quellknoten und idempotente Wiederholung;
- Last-Write-Wins pro Primärschlüssel für Tabellen mit Zeitstempel;
- Toleranz für Schema-Drift durch gemeinsame Spalten;
- konfigurierbare Ausschlüsse und Entfernung sensibler Tabelleninhalte aus Snapshots
  mit anschließendem VACUUM;
- eigene Merge-Policy für Tombstones, CRDTs oder fachliche Konfliktregeln;
- Python-API und JSON-CLI ohne zusätzliche Laufzeitabhängigkeiten.

## Kurzstart

```powershell
python -m pip install -e .
sqlite-transit-sync init --config node.json --database app.db `
  --transit shared-transit --node-id laptop --namespace meine-app
sqlite-transit-sync push --config node.json
sqlite-transit-sync pull --config node.json --dry-run
sqlite-transit-sync pull --config node.json
sqlite-transit-sync verify --config node.json
```

Das Anwendungsschema muss auf jedem Knoten bereits existieren. Ein automatisches
Erstkopieren ist absichtlich deaktiviert, weil nur die Anwendung entscheiden kann,
welche Migrationen, lokalen Tabellen und Geheimnisse zulässig sind.

## Vergleich mit Distributed SQL

| Aspekt | `sqlite-transit-sync` | Distributed SQL, zum Beispiel CockroachDB oder YugabyteDB |
|---|---|---|
| Grundmodell | Jeder Knoten besitzt eine unabhängige lokale SQLite-Datenbank | Alle Server bilden gemeinsam eine logische SQL-Datenbank |
| Schreibzugriff | Zunächst lokal, anschließend synchronisiert | Direkt durch den Cluster koordiniert |
| Synchronisierung | Zeitversetzter Snapshot-Pull mit Zeilen-Merge | Laufende Replikation zwischen Clusterknoten |
| Konsistenz | Eventual Consistency nach erfolgreichem Austausch | Üblicherweise starke oder serialisierbare Konsistenz |
| Konsens und Quorum | Nicht erforderlich | Meist Raft-basierter Mehrheitskonsens |
| Globale Transaktionen | Nein | Ja, auch über mehrere Knoten oder Datenbereiche |
| Konfliktbehandlung | Anwendungsspezifische `MergePolicy`; standardmäßig Timestamp-LWW | Transaktionen, MVCC, Sperren und Konsens |
| Offline-Betrieb | Jeder Knoten kann unabhängig weiterarbeiten | Schreibzugriffe benötigen normalerweise ein erreichbares Quorum |
| Netzwerkausfall | Lokale Arbeit läuft weiter; der Abgleich wartet | Minderheitsknoten können ihre Schreibfähigkeit verlieren |
| Ausfallsicherheit | Lokale DBs bleiben nutzbar; Transit und Backups brauchen eigene Absicherung | Replikation und automatisches Failover bei vorhandenem Quorum |
| Sichtbarkeit | Änderungen werden nach Push und Pull gemeinsam sichtbar | Bestätigte Änderungen gelten unmittelbar im Cluster |
| Schemaänderungen | Die Anwendung migriert jede lokale Datenbank kontrolliert | Clusterweit koordinierte SQL-Migrationen |
| Löschungen | Benötigen Tombstones oder eine eigene Policy | Normale transaktionale SQL-Löschung |
| Infrastruktur | Python, SQLite und ein konfigurierbarer Dateitransport | Mehrere dauerhafte DB-Server, TLS, Monitoring und Backups |
| Ständig aktive Server | Keine Mindestzahl; ein Knoten genügt | Für Fehlertoleranz üblicherweise mindestens drei |
| Wichtigster Vorteil | Offline-Fähigkeit, niedriger Aufwand und fachliche Merge-Regeln | Starke Konsistenz, parallele Writer und Hochverfügbarkeit |
| Wichtigster Nachteil | Keine globale ACID-Transaktion oder sofortige gemeinsame Wahrheit | Deutlich höherer Betriebs- und Ressourcenaufwand |

### Vorteile, Nachteile und typische Use Cases

| System | Vorteile | Nachteile | Geeignete Use Cases | Ungeeignete Use Cases |
|---|---|---|---|---|
| `sqlite-transit-sync` | Sehr geringer Ressourcenbedarf; offlinefähig; kein zentraler Server; lokale Datenhaltung; freier Transportweg; fachlich anpassbare Konfliktregeln | Änderungen werden verzögert sichtbar; Konflikte, Löschungen, Zeitstempel und Migrationen bleiben Anwendungsverantwortung; keine globalen ACID-Transaktionen und kein Quorum-Failover | Persönliche Wissens- und Taskdatenbanken; lokale KI-Agenten; Laptop–Workstation–Server-Abgleich; Außen- und Edge-Anwendungen; Desktop-Software mit optionalem Sync; Forschungsnotizen | Zahlungen, knappe Lagerbestände, Sitzplatzreservierungen, Echtzeit-Kollaboration am selben Datensatz oder viele konkurrierende Writer |
| Distributed SQL | Gemeinsame verbindliche Datenbank; starke Konsistenz; globale Transaktionen; koordinierte parallele Writes; automatische Replikation und Failover; horizontale Skalierung | Dauerhafte Server, Netzwerk, Zertifikate, Monitoring und Upgrades erforderlich; Quorum kann bei Partitionen Writes blockieren; höhere Latenz und Kosten | Finanz- und Buchungssysteme; SaaS-Plattformen; E-Commerce-Bestand; globale Benutzerkonten; Multiplayer-Backends; hochverfügbare Unternehmensdienste | Kleine persönliche Tools, zeitweise getrennte Geräte, Einzelbenutzer-Desktop-Anwendungen oder bereits zuverlässig durch lokale SQLite-DBs abgedeckte Workloads |

### Schnelle Entscheidungshilfe

| Anforderung | Passender Ansatz |
|---|---|
| Geräte müssen offline weiterarbeiten | `sqlite-transit-sync` |
| Änderungen dürfen erst nach einem Sync gemeinsam sichtbar werden | `sqlite-transit-sync` |
| Daten sollen lokal bleiben und Konflikte sind selten | `sqlite-transit-sync` |
| Viele Clients verändern dieselben Datensätze gleichzeitig | Distributed SQL |
| Jeder Commit muss sofort global verbindlich sein | Distributed SQL |
| Globale Transaktionen oder automatisches Cluster-Failover sind Pflicht | Distributed SQL |

Für wenige zeitweise verbundene persönliche oder Edge-Geräte ist
`sqlite-transit-sync` meist die einfachere Lösung. Sobald echte konkurrierende
Writer entstehen, ist häufig eine zentrale PostgreSQL-Instanz der nächste sinnvolle
Schritt. Distributed SQL wird interessant, wenn starke Konsistenz zusätzlich den
Ausfall einzelner Server über mehrere dauerhaft betriebene Knoten überstehen muss.

## Wichtige Grenzen

- Aktive Datenbanken bleiben immer lokal und liegen niemals im Transportordner.
- SHA-256 erkennt Beschädigung, authentifiziert aber keinen feindlichen Transport.
- Die Standardregel synchronisiert keine Löschungen und setzt vergleichbare
  Zeitstempel voraus.
- Gleiche Zeitstempel konvergieren über einen deterministischen Inhaltsvergleich;
  fachliche Konfliktregeln kann dieser technische Fallback nicht ersetzen.
- Tabellen ohne Primärschlüssel oder passende Zeitstempelspalte werden übersprungen.
- Snapshot-Redaktion löscht gelistete Tabellen und führt danach VACUUM aus; sensible
  Tabellen müssen trotzdem vollständig in `snapshot_exclude_tables` angegeben sein.
- Aufbewahrung, Migrationen und fachliche Konfliktregeln verantwortet die Anwendung.
- Pro Knoten darf ohne zusätzlichen Prozess-Lock nur ein Sync-Prozess gleichzeitig laufen.

Die vollständige API- und Konfigurationsreferenz steht in [README.md](README.md).
