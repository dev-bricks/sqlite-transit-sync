# sqlite-transit-sync

<img src="assets/banner.png" width="100%" alt="Sqlite Transit Sync banner">


[English](README.md) | [Deutsch](README_de.md)

[![License](https://img.shields.io/github/license/ellmos-ai/sqlite-transit-sync)](LICENSE)
[![Python Version](https://img.shields.io/badge/python->=3.10-blue.svg)](https://www.python.org/)
[![Ecosystem: dev-bricks](https://img.shields.io/badge/Ecosystem-dev--bricks-blue.svg)](https://github.com/dev-bricks)
[![Umbrella: open-bricks](https://img.shields.io/badge/Umbrella-open--bricks-purple.svg)](https://github.com/open-bricks)
[![Architecture](https://img.shields.io/badge/architecture-local--first-success.svg)](#teil-der-ellmos-stack-familie)
[![Tests](https://img.shields.io/badge/tests-55%2F55%20passed-brightgreen.svg)](#tests)
[![llms.txt](https://img.shields.io/badge/llms.txt-available-informational.svg)](llms.txt)

> [!NOTE]
> **Kontext für LLMs und KI-Agenten**: Ein strukturierter maschinenlesbarer Verzeichnisbaum, ein Architekturüberblick und ein API-Leitfaden stehen unter [`llms.txt`](llms.txt) bereit.

Local-first-Synchronisierung für unabhängige SQLite-Datenbanken über geprüfte
Snapshots und von der Anwendung wählbare Merge-Policies. Das Modul wurde aus der
BACH-ProSync-Architektur extrahiert und enthält keine Abhängigkeiten von BACH,
OneDrive, Rechnernamen oder Benutzerpfaden.

Dies ist **kein verteilter SQL-Server**. Jeder Knoten besitzt und öffnet nur seine
lokale Datenbank. Ein gemeinsamer Ordner, ein eingebundener Object Store, ein
Wechseldatenträger oder ein anderer Dateitransport überträgt geschlossene Snapshots
mit Manifesten. Beim Pull wird ein Snapshot geprüft und innerhalb einer Transaktion
in die lokale Datenbank zusammengeführt.

Passt gut zu [sync-master](https://github.com/dev-bricks/sync-master) aus derselben
Modulfamilie: Ein sync-master-Yard ist ein natürlicher Transit-Transport. Dazu wird
`--transit` auf eine werkzeugeigene Zone `db-transit/<namespace>/` im Yard gesetzt
(dort Protokollregel R9). sync-master transportiert die Dokumente, dieses Modul
verantwortet Datenbankintegrität und Merge; beide bleiben unabhängig.

## Architektur & Datenfluss

```mermaid
sequenceDiagram
    autonumber
    participant NodeA as Node A (app.db)
    participant Transit as Shared Transit (db-transit)
    participant NodeB as Node B (app.db)

    Note over NodeA: Live local writes
    NodeA->>NodeA: SQLite Backup API (Online Snapshot)
    NodeA->>Transit: Push atomic snapshot & SHA-256 manifest
    Note over Transit: Verified Transit Storage (R9)
    NodeB->>Transit: Read manifest & PRAGMA quick_check
    NodeB->>NodeB: Transactional Row Merge (LWW / Policy)
    Note over NodeB: Eventual Consistency Reached
```

## Teil der ellmos-Stack-Familie

`sqlite-transit-sync` ist ein Companion-Modul zu
[dev-bricks/sync-master](https://github.com/dev-bricks/sync-master) aus derselben
Modulfamilie: sync-master verantwortet den Dateitransport (`sync.files`), dieses
Modul die Datenbankintegrität und den Merge (`sync.database`). Beide sind
eigenständig oder als Teil von Stacks aus der
[ellmos-ai](https://github.com/ellmos-ai)-Familie nutzbar; siehe den
[ellmos-ai/stacks](https://github.com/ellmos-ai/stacks)-Katalog. Seine Rolle in
diesem Baukasten: kein Live-SQLite über Dateisynchronisierung, sondern ausschließlich
geprüfte Snapshots mit von der Anwendung wählbaren Merge-Policies.

## Eigenschaften

- konsistente Online-Snapshots über die SQLite-Backup-API;
- atomare Veröffentlichung über eine temporäre Datei und `os.replace`;
- geschlossene Snapshots im Rollback-Journal-Modus mit Fail-closed-Bereinigung aller
  temporären SQLite-Sidecars vor der Veröffentlichung;
- SHA-256-Manifest und Prüfung mit `PRAGMA quick_check`;
- lokaler Pull-Zustand je Knoten und idempotente Wiederholung;
- zeilenweises Last-write-wins pro Primärschlüssel für Tabellen mit Zeitstempel;
- Merge gemeinsamer Spalten für grundlegende Toleranz gegenüber Schema-Drift;
- konfigurierbare Tabellenausschlüsse und Snapshot-Redaktion mit anschließendem
  `VACUUM`;
- inhaltsbezogener Credential-Scan, der die Veröffentlichung abbricht, wenn ein
  Snapshot weiterhin zugangsdatenähnliche Werte enthält (standardmäßig aktiv;
  gemeldet wird `table.column`, niemals der Wert selbst);
- eigene `MergePolicy` für fachliche Regeln, Tombstones oder CRDTs;
- optionaler [Republica-Schaufenster-Modus](#republica--die-schaufenster-methode), der eine Datenbank
  einseitig als verschlüsselte Nutzlast verteilt und als eigenständige,
  schreibgeschütztes Schaufenster materialisiert, statt sie zu mergen;
- Python-API und JSON-CLI ohne zusätzliche Laufzeitabhängigkeiten (der Republica-Modus
  ergänzt `cryptography`).

## Installation

```bash
python -m pip install -e .
```

## Kurzstart

Auf jedem Knoten wird eine Konfiguration angelegt. Jeder Knoten verwendet seine
eigene Datenbank und Zustandsdatei, aber dasselbe Transit-Verzeichnis und denselben
Namespace.

```bash
sqlite-transit-sync init \
  --config node.json \
  --database ./app.db \
  --transit ./shared-transit \
  --node-id laptop \
  --namespace my-app

sqlite-transit-sync push --config node.json
sqlite-transit-sync pull --config node.json --dry-run
sqlite-transit-sync pull --config node.json
sqlite-transit-sync status --config node.json
sqlite-transit-sync verify --config node.json
```

Das Anwendungsschema muss auf jedem Knoten bereits existieren. Ein automatisches
Erstkopieren ist absichtlich deaktiviert, weil ein generisches Modul nicht
entscheiden kann, welches Schema, welche Secrets, lokalen Tabellen oder Migrationen
zu einer Anwendung gehören.

## Konfiguration

```json
{
  "database": "./app.db",
  "transit": "./shared-transit",
  "state": "./node-state.json",
  "node_id": "laptop",
  "namespace": "my-app",
  "timestamp_columns": ["updated_at", "modified_at", "created_at"],
  "exclude_tables": ["secrets", "sqlite_sequence"],
  "snapshot_exclude_tables": ["secrets"],
  "scan_snapshot_for_secrets": true,
  "secret_scan_skip_tables": [],
  "secret_scan_extra_patterns": [],
  "secret_patterns_file": null
}
```

Relative Pfade werden vom Speicherort der Konfigurationsdatei aus aufgelöst. Die
aktive Datenbank darf niemals innerhalb des Transit-Verzeichnisses liegen.

Anwendungen, die einen Audit-Hash für genau die eingelesene Konfiguration benötigen,
können die Bytes einmal lesen und mit demselben quellrelativen Parser verwenden,
ohne die Datei ein zweites Mal einzulesen:

```python
import hashlib
from pathlib import Path

from sqlite_transit_sync import SyncConfig

config_path = Path("node.json").resolve()
payload = config_path.read_bytes()
config = SyncConfig.from_bytes(payload, source_path=config_path)
config_sha256 = hashlib.sha256(payload).hexdigest()
```

### Alle Schlüssel und ihre Standardwerte

| Schlüssel | Standardwert | Bedeutung |
|---|---|---|
| `database` | *(erforderlich)* | Aktive SQLite-Datenbank dieses Knotens |
| `transit` | *(erforderlich)* | Gemeinsames Verzeichnis für Snapshots und Manifeste |
| `state` | `./.sync-state.json` | Pull-Zustand dieses Knotens; außerhalb des Transits aufbewahren |
| `node_id` | Rechnername | Identifiziert den veröffentlichenden Knoten in Snapshot-Namen |
| `namespace` | `"default"` | Trennt unabhängige Datensätze innerhalb eines Transits |
| `timestamp_columns` | `["updated_at","modified_at","created_at"]` | Für Last-write-wins geprüfte Spalten |
| `exclude_tables` | `["secrets","sqlite_sequence"]` | Tabellen, die niemals in die lokale Datenbank **gemergt** werden |
| `snapshot_exclude_tables` | `["secrets"]` | Tabellen, deren Zeilen vor der Veröffentlichung aus dem Snapshot **gelöscht** werden, gefolgt von `VACUUM` |
| `scan_snapshot_for_secrets` | `true` | Bricht die Veröffentlichung ab, wenn der Snapshot-Inhalt weiterhin wie Zugangsdaten aussieht |
| `secret_scan_skip_tables` | `[]` | Tabellen, die der Scan auslässt; bei einer einzelnen störenden Tabelle besser als die vollständige Abschaltung |
| `secret_scan_extra_patterns` | `[]` | Zusätzliche reguläre Ausdrücke, ergänzend zur Trigger-Datei |
| `secret_patterns_file` | `null` (mitgelieferte Datei) | Pfad zu einer eigenen Trigger-Datei; **ersetzt** die eingebauten Muster |
| `key_file` | `null`, sonst `$SQLITE_TRANSIT_SYNC_KEY_FILE` | Fernet-Schlüssel für den Republica-Modus; muss außerhalb des Transits liegen |
| `republica_root` | `~/.republica` | Ablageort importierter Schaufenster; muss außerhalb des Transits liegen |
| `allow_key_in_synced_folder` | `false` | Hebt die Prüfung auf, die einen Schlüssel in einem Cloud-Ordner ablehnt |

Der `state`-Standardwert in dieser Tabelle gilt, wenn eine JSON-Konfiguration ohne
diesen Schlüssel geladen wird. `sqlite-transit-sync init` schreibt ohne `--state`
stattdessen `.<config-stem>-state.json` (für `node.json`: `.node-state.json`).

### Der Credential-Scan und wie man ihn abschaltet

`snapshot_exclude_tables` kann nur Tabellen entfernen, die bereits bekannt sind.
Der Scan beantwortet die darüber hinausgehende Frage: *Sind Zugangsdaten in einer
Freitextspalte gelandet* – etwa in einer Notiz, Logzeile oder Sitzungszusammenfassung?
Er läuft nach der Redaktion und vor der Veröffentlichung auf der Snapshot-Kopie.
Bei einem Treffer löst er einen `SyncError` mit `table.column` aus; der gefundene
Wert wird niemals ausgegeben und gelangt daher nicht in Logs, Tracebacks oder
CI-Ausgaben. Der unvollständige Snapshot wird verworfen, sodass nichts das
Transit-Verzeichnis erreicht.

**Das Abschalten ist eine legitime Entscheidung**, kein Notbehelf. Wer den
Transport selbst kontrolliert und ihm vertraut – eigener Server, EU-gehostetes
Volume unter eigenem Vertrag oder verschlüsselter Wechseldatenträger – und
Zugangsdaten bewusst mit den Daten transportieren möchte, setzt:

```json
{ "scan_snapshot_for_secrets": false }
```

Wenn nur eine Tabelle Fehlalarme erzeugt, sollte `secret_scan_skip_tables` verwendet
werden. Der Schutz bleibt dann für alle anderen Tabellen aktiv.

### Trigger anpassen

Die Muster liegen als Daten statt im Code unter
`sqlite_transit_sync/credential-triggers.json`. Dadurch lässt sich die Erkennung
verschärfen, ohne auf ein Release zu warten:

```json
{
  "version": 1,
  "patterns": [
    { "name": "github", "regex": "gh[pousr]_[A-Za-z0-9]{16,}", "prefilter": "gh" },
    { "name": "acme-internal", "regex": "ACME-[0-9]{4}", "prefilter": "ACME-" }
  ]
}
```

- `prefilter` ist ein optionales Literal für einen schnellen SQL-`LIKE`-Vorfilter,
  damit große Snapshots schnell bleiben. Es **muss** in jedem Wert vorkommen, auf
  den der reguläre Ausdruck passt; andernfalls werden Funde übersehen. Fehlt ein
  solches Literal, wird die gesamte Spalte gelesen, damit die Prüfung korrekt bleibt.
- Mit `secret_patterns_file` ersetzt eine eigene Datei die Standardmuster
  vollständig; `secret_scan_extra_patterns` ergänzt sie.

Die Muster sind bewusst herstellerpräfixiert. Eine allgemeine Regel für „lange
hexadezimale Zeichenfolgen“ würde Prüfsummen, UUIDs und Git-SHAs erfassen, obwohl
diese legitime Datenbankinhalte sind. Ein Scanner mit vielen Fehlalarmen wird
abgeschaltet und schützt dann gar nicht mehr. Ein sauberer Scan bedeutet „kein
bekanntes Muster gefunden“, niemals „dieser Snapshot enthält garantiert keine
Secrets“.

## Republica — die Schaufenster-Methode

Jede Maschine stellt ein **verschlüsseltes Schaufenster** ihrer Datenbank in eine geteilte
Dateifläche. Alle anderen können hineinsehen, niemand kann es verändern. Daher der Name: eine
Wiederveröffentlichung der Datenbank, lesbar nur für den, der den Schlüssel hat.

Zu verwenden, wenn `push`/`pull` nicht passt: Man möchte *lesen*, was ein anderer Knoten
weiß, ohne es in die eigenen Zeilen einzurechnen — oder die Maschinen teilen nichts als einen
Ordner (kein Server, keine offenen Ports, kein Vertrauens-Setup), und dieser Ordner soll die
Inhalte nicht im Klartext sehen.

### Zwei Betriebsarten, bewusst redundant

Republica ist **keine Übergangslösung, bis ein richtiger Tunnel steht.** Es ist die zweite
von zwei Betriebsarten, die nebeneinander laufen sollen, damit der Ausfall der einen die
andere nicht stoppt:

| Ausfall | Direkter Abgleich (`push`/`pull`) | Republica |
|---|---|---|
| Eine Maschine schläft oder ist offline | steht still (kein Gegenüber) | läuft weiter — jetzt ablegen, später abholen |
| VPN/SSH tot, Netz blockiert den Tunnel | steht still | läuft weiter über die Dateifläche |
| Schlüsselrotation oder Trust-Setup offen | steht still | läuft weiter mit dem geteilten Schlüssel |
| Geteilter Ordner kaputt, voll oder desynchron | läuft weiter | steht still |
| Keine Merge-Regel für einen Datensatz vereinbart | nicht anwendbar | läuft weiter — es wird nichts gemergt |

Der ganze Nutzen liegt darin, dass es an dem Tag funktioniert, an dem der andere Weg es nicht
tut. Also eingerichtet lassen und mitlaufen lassen, auch wenn der direkte Weg gerade
problemlos läuft.

### Einrichtungskosten: ein einziger Schlüsseltransfer

Der geteilte Schlüssel muss die anderen Maschinen über **irgendeinen Kanal erreichen, der
nicht der Transportweg selbst ist** — ein bestehender verschlüsselter Tunnel, ein
Passwortmanager, ein USB-Stick, telefonisch vorgelesen. Einmal. Danach genügt ein schlichter
geteilter Ordner, dauerhaft, selbst einer, dem man nicht vertraut.

```text
republica_root/
  laptop/my-app.sqlite      <- schreibgeschütztes Schaufenster der Laptop-Datenbank
  workstation/my-app.sqlite <- schreibgeschütztes Schaufenster der Workstation-Datenbank
```

```bash
# Einmalig, auf lokaler Platte - nie im Transit, nie in einem Cloud-Ordner.
# Diese Datei dann out-of-band auf die anderen Maschinen kopieren; dort NICHT keygen aufrufen.
sqlite-transit-sync keygen --key-file ~/.keys/republica.key

sqlite-transit-sync init --config node.json \
  --database ./app.db --transit ./shared-transit \
  --node-id laptop --namespace my-app \
  --key-file ~/.keys/republica.key

sqlite-transit-sync republica-publish --config node.json   # verschlüsseln und veröffentlichen
sqlite-transit-sync republica-list    --config node.json   # was andere Knoten anbieten
sqlite-transit-sync republica-import  --config node.json   # lokal materialisieren
```

Benötigt die optionale Verschlüsselung: `pip install 'sqlite-transit-sync[crypto]'`.

**Übertragen wird** keine Datenbankdatei, sondern ein kuratierter SQL-Dump, gzip-komprimiert
und Fernet-verschlüsselt. Bei einer realen 53,6-MB-Wissensdatenbank sind das 11,0 MB im
Transit, weil die Interna des Volltextindex beim Import neu aufgebaut statt mitgeschickt
werden (35.370 von 49.636 Dump-Anweisungen). Die Veröffentlichung durchläuft dieselbe
Prüfkette wie ein Merge-Snapshot: Redaktion, Credential-Scan, `quick_check`, Manifest.

**Was das schützt:** Der Transport sieht nur Chiffretext, und Fernets HMAC erkennt eine
absichtliche Änderung selbst dann, wenn der Manifest-Hash passend nachgerechnet wurde.

**Was nicht:** Fernet authentifiziert den *Schlüssel*, nicht den *Absender* – wer ihn
besitzt, kann einen gültigen Snapshot veröffentlichen. Genau deshalb bleibt eine Replica
getrennt und wird nie gemergt. Der Schlüssel gehört nicht in den Transportweg: ein Schlüssel
im Transitverzeichnis wird abgelehnt, ebenso einer in einem erkennbar synchronisierten
Ordner (abschaltbar über `allow_key_in_synced_folder`). Dasselbe gilt für `republica_root` –
eine entschlüsselte Replica im Transit würde im Klartext weiterverteilt.

**Grenze:** Ein Volltextindex ohne eigenen Inhalt (`content=''`) lässt sich nicht neu
aufbauen, weil die Quelle dafür fehlt. Solche Tabellen meldet das Manifest unter
`contentless_fts`, statt still leer anzukommen.

### Sealed Envelope: eine Datei, derselbe Kanal, niemals eine Datenbank

Das Henne-Ei-Problem: Zwei Maschinen teilen *noch* keinen sicheren Kanal — und genau deshalb
muss ein Zugangsdatum hinüber. Derselbe Schlüssel und derselbe Ordner befördern auch eine
einzelne verschlüsselte Datei.

```bash
sqlite-transit-sync envelope-send    --config node.json --file ./api-token.txt --label api-token
sqlite-transit-sync envelope-receive --config node.json --into ~/credentials
```

Die Datei kommt **als Datei** an (Rechte `0600`), benannt `<quellknoten>__<dateiname>`, und
landet **nie in einer Datenbank** — ein Geheimnis in einer Datenbank wird von jedem Backup,
jedem Index und jeder Synchronisierung weiterkopiert, die sie berührt. In Notizen gehört der
Fundort, niemals das Geheimnis selbst.

Zwei Regeln des Schaufensters sind hier bewusst umgekehrt: Der **Credential-Scan gilt nicht**
(er würde genau die Fracht blockieren, die befördert werden soll), und der Umschlag wird nach
dem Empfang **aus dem Transit entfernt**, damit ein Geheimnis nicht im geteilten Ordner liegen
bleibt. Das Entpacken in den Transit oder in einen erkennbar cloud-synchronisierten Ordner
wird verweigert, und der Dateiname wird beim Empfang erneut entschärft, damit ein
manipuliertes Manifest nicht außerhalb des Zielverzeichnisses schreiben kann.

Das ist ein Kurier, kein Passwort-Manager und keine Dateisynchronisierung — wenige, kleine
Umschläge.

## Das hier ist kein Passwort-Manager

Der Scan **entfernt** Zugangsdaten aus dem Synchronisierungsweg. Er **verteilt** sie
nicht. Wenn das eigentliche Problem lautet „meine Rechner benötigen dieselben
Passwörter oder API-Schlüssel“, ist dieses Modul das falsche Werkzeug – ebenso wie
jeder Dokumenten-Synchronisierungsordner. Stattdessen sollte einer der folgenden
Ansätze verwendet werden; alle halten Klartext von einem nicht selbst
kontrollierten Anbieter fern:

| Ansatz | Geeignet für | Hinweise |
|---|---|---|
| **Vaultwarden** (selbst gehostetes Bitwarden) | Menschen und CLI auf mehreren Rechnern | Läuft auf einem kleinen ständig aktiven Rechner; Zugriff über ein privates Netz wie WireGuard oder Tailscale statt über eine öffentliche Freigabe. Offizielle Bitwarden-Clients, Browser-Erweiterungen und die `bw`-CLI funktionieren damit, sodass auch Skripte und Agenten Secrets abrufen können. |
| **SOPS + age** | Secrets neben dem Code | Verschlüsselte Dateien können sicher committet oder in beliebige Synchronisierungsordner gelegt werden, weil nur Chiffrat übertragen wird. Unterstützt Schlüssel je Empfänger und eignet sich gut für Git-Reviews. |
| **`pass`** (GPG) + Git | Unix-orientierte Einzelnutzer und kleine Teams | Eine Datei pro Secret, normales Git-Remote, kein Server erforderlich. |
| **KeePassXC-Datenbank über Syncthing** | kein Server, kein Cloud-Konto | Peer-to-Peer-Dateisynchronisierung; der Tresor bleibt eine einzelne verschlüsselte Datei. |
| **Infisical / OpenBao (Vault-Fork)** | Teams, Maschinenidentitäten und Rotation | Echte Secret-Server mit Audit-Logs und dynamischen Zugangsdaten; mehr bewegliche Teile, als ein Haushalt benötigt. |
| **Plattformeigene Speicher** | ein Rechner, eine Anwendung | macOS-Schlüsselbund, Windows DPAPI/Anmeldeinformationsverwaltung, `systemd-creds` oder der Secret-Store der CI. Kein Abgleich, aber auch keine Offenlegung. |

Unabhängig von der Wahl bleibt dieselbe Trennung entscheidend: **ein Kanal für
Daten, ein anderer für Zugangsdaten.** Die Aufgabe dieses Moduls besteht darin,
sicherzustellen, dass der erste Kanal nicht unbemerkt zum zweiten wird – genau das
erzwingt der Scan.

## Python-API

```python
from sqlite_transit_sync import SyncConfig, TransitSync

sync = TransitSync(SyncConfig.from_file("node.json"))
snapshot = sync.push()
reports = sync.pull()
print(snapshot.sha256, [report.as_dict() for report in reports])
```

Wenn Timestamp-LWW nicht ausreicht, kann `TransitSync` ein Objekt erhalten, das
`MergePolicy.merge(local, remote, snapshot)` implementiert.

## Vergleich mit Distributed SQL

| Aspekt | `sqlite-transit-sync` | Distributed SQL, zum Beispiel CockroachDB oder YugabyteDB |
|---|---|---|
| Grundmodell | Jeder Knoten besitzt eine unabhängige lokale SQLite-Datenbank | Alle Server bilden gemeinsam eine logische SQL-Datenbank |
| Schreibzugriff | Zunächst lokal, später synchronisiert | Direkt durch den Cluster koordiniert |
| Synchronisierung | Asynchroner Snapshot-Pull mit Zeilen-Merge | Laufende Replikation zwischen Clusterknoten |
| Konsistenz | Eventual Consistency nach erfolgreichem Austausch | Üblicherweise starke oder serialisierbare Konsistenz |
| Konsens und Quorum | Nicht erforderlich | Meist Raft-basierter Mehrheitskonsens |
| Globale Transaktionen | Nein | Ja, auch über mehrere Knoten oder Shards |
| Konfliktbehandlung | Anwendungsspezifische `MergePolicy`; standardmäßig Timestamp-LWW | Transaktionen, MVCC, Sperren und Konsens |
| Offline-Betrieb | Ein Knoten kann unabhängig weiter lesen und schreiben | Schreibzugriffe benötigen normalerweise ein erreichbares Quorum |
| Netzwerkausfall | Lokale Arbeit läuft weiter; die Synchronisierung wartet | Minderheitspartitionen können ihre Schreibfähigkeit verlieren |
| Ausfallsicherheit | Lokale Datenbanken bleiben nutzbar; Transit und Backups benötigen eigene Absicherung | Replikation und automatisches Failover, solange ein Quorum verfügbar ist |
| Sichtbarkeit | Änderungen werden nach Push und Pull gemeinsam sichtbar | Bestätigte Änderungen sind im Cluster unmittelbar autoritativ |
| Schemaänderungen | Die Anwendung migriert jede lokale Datenbank | Clusterweite SQL-Migrationen |
| Löschungen | Benötigen Tombstones oder eine eigene Policy | Normale transaktionale SQL-Löschungen |
| Infrastruktur | Python, SQLite und ein konfigurierbarer Dateitransport | Mehrere dauerhafte Datenbankserver, TLS, Monitoring und Backups |
| Mindestzahl ständig aktiver Server | Keine; ein Knoten genügt | Für Fehlertoleranz üblicherweise mindestens drei |
| Wichtigster Vorteil | Offline-first-Einfachheit, niedrige Kosten und fachliche Merge-Regeln | Starke Konsistenz, parallele Writer und Hochverfügbarkeit |
| Wichtigste Grenze | Keine globale ACID-Transaktion oder sofortige gemeinsame Wahrheit | Deutlich höherer Betriebs- und Ressourcenaufwand |

### Vorteile, Nachteile und typische Use Cases

| System | Vorteile | Nachteile | Geeignete Use Cases | Ungeeignete Use Cases |
|---|---|---|---|---|
| `sqlite-transit-sync` | Sehr geringer Ressourcenbedarf; offlinefähig; kein zentraler Server; lokale Datenhaltung; transportunabhängig; Merge-Regeln können der Fachdomäne folgen | Änderungen werden verzögert sichtbar; Konflikte, Löschungen, Zeitregeln und Migrationen bleiben Anwendungsverantwortung; keine globalen ACID-Transaktionen und kein Quorum-Failover | Persönliche Wissens- und Taskdatenbanken; lokale KI-Agenten; Laptop-, Workstation- und Serveraustausch; Außen- und Edge-Anwendungen; Desktop-Software mit optionaler Synchronisierung; Forschungsnotizen | Zahlungen, knappe Lagerbestände, Sitzplatzreservierungen, Echtzeit-Zusammenarbeit am selben Datensatz oder viele konkurrierende Writer |
| Distributed SQL | Gemeinsame autoritative Datenbank; starke Konsistenz; globale Transaktionen; koordinierte parallele Schreibzugriffe; automatische Replikation und Failover; horizontale Skalierung | Dauerhafte Server, Netzwerk, Zertifikate, Monitoring und Upgrades erforderlich; Quorum kann bei Partitionen Schreibzugriffe blockieren; höhere Latenz und Kosten | Finanz- und Buchungssysteme; SaaS-Plattformen; E-Commerce-Bestand; globale Benutzerkonten; Multiplayer-Backends; hochverfügbare Unternehmensdienste | Kleine persönliche Werkzeuge, zeitweise getrennte Geräte, Einzelbenutzer-Desktop-Anwendungen oder bereits zuverlässig durch lokale SQLite-Datenbanken abgedeckte Workloads |

### Schnelle Entscheidungshilfe

| Anforderung | Bevorzugter Ansatz |
|---|---|
| Knoten müssen offline weiterarbeiten | `sqlite-transit-sync` |
| Änderungen dürfen erst nach einem Synchronisierungsschritt sichtbar werden | `sqlite-transit-sync` |
| Daten sollen lokal bleiben und Konflikte sind selten | `sqlite-transit-sync` |
| Viele Clients verändern dieselben Datensätze gleichzeitig | Distributed SQL |
| Jeder Commit muss sofort global autoritativ sein | Distributed SQL |
| Globale Transaktionen oder automatisches Cluster-Failover sind Pflicht | Distributed SQL |

Für wenige zeitweise verbundene persönliche oder Edge-Geräte ist
`sqlite-transit-sync` meist die einfachere Lösung. Sobald echte konkurrierende
Writer entstehen, ist häufig eine zentrale PostgreSQL-Instanz der nächste sinnvolle
Schritt. Distributed SQL wird interessant, wenn starke Konsistenz zusätzlich den
Ausfall einzelner Server über mehrere dauerhaft betriebene Knoten überstehen muss.

## Sicherheit und Grenzen

- Eine aktive SQLite-Datenbank niemals aus einem Netzwerk- oder
  Cloud-Synchronisierungsordner öffnen.
- SHA-256 erkennt Beschädigung, authentifiziert aber keinen feindlichen Transport.
- Das standardmäßige LWW setzt vergleichbare Zeitstempel voraus und leitet keine
  Löschungen ab.
- Gleiche Zeitstempel konvergieren über einen deterministischen Inhaltsvergleich.
  Dieser technische Fallback ersetzt keine fachlichen Konfliktregeln.
- Tabellen ohne Primärschlüssel oder Zeitstempelspalte werden übersprungen.
- Snapshot-Redaktion löscht gelistete Tabellen und führt anschließend `VACUUM` aus.
  Trotzdem muss jede Tabelle mit Zugangsdaten oder privaten Daten gelistet werden;
  das generische Modul kann fachliche Secrets nicht zuverlässig erkennen.
- Anwendungsmigrationen, Clock Policy, Aufbewahrung und Konfliktsemantik bleiben bei
  der integrierenden Anwendung.
- Pro Knoten darf ohne zusätzlichen Prozess-Lock der Host-Anwendung nur ein
  Synchronisierungsprozess laufen.

Siehe [ARCHITECTURE.md](ARCHITECTURE.md), [README.md](README.md) und
[SECURITY.md](SECURITY.md).

<!-- BEGIN ELLMOS BUNDLE DISCOVERY DE -->

## Bundles und Partner

Geprüfte Discovery-Projektion für `module:sqlite-transit-sync` aus
`catalog:v4-bundles`
(`a52688938bcad21469beb546acfe6dd79ca40196a2bbaf246e5bd6aaac4bbbd7`).
Das Ziel-Repository ist `public`. Die Bundle-Manifeste bleiben die Autorität
für Mitgliedschaften; dieser Abschnitt installiert oder aktiviert keine
Komponenten. Die Freigabe beruht auf einem öffentlichen Modul-Registry-Eintrag
und einer ausdrücklichen Default-deny-Allowlist für Bundles.

### `ellmos-sync-federation-bundle`

- Sichtbarkeit des Bundle-Rezepts: `private`; Rolle: `declared-component`;
  Anforderung: `recommended`.
- Modulpartner: `module:cloud-safe-exporter`, `module:receipt-validator`,
  `module:sync`, `module:system-explorer-export`, `module:system-gap-master`.
- Skill-Partner: `skill:agent-config-sync`, `skill:mcp-config-sync`,
  `skill:system-onboarding`.

Kompositions- und Runtime-Details werden bewusst nicht offengelegt.

<!-- END ELLMOS BUNDLE DISCOVERY DE -->

## Maschinenlesbarer Index

Für KI-Agenten, LLMs und automatisierte Werkzeuge steht unter [llms.txt](llms.txt)
ein strukturierter Verzeichnisbaum mit API-Index bereit.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Herkunft

Das Modul wurde 2026 aus BACH `system/hub/db_sync.py` (ProSync) extrahiert. Die
eigenständige Fassung ersetzt BACH-spezifische Pfade, Handler, Secrets und
Tabellenannahmen durch Konfigurations- und Policy-Schnittstellen. Sie führt den
Merge außerdem je Primärschlüssel aus und ergänzt geprüfte Manifeste.

MIT – siehe [LICENSE](LICENSE).
