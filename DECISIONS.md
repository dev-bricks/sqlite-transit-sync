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

**Jedes Herstellerpräfix ist links verankert** (`(?<![A-Za-z0-9])`). Ohne diesen Anker ist die
Regel nicht „präzise pro Hersteller", sondern eine Teilstring-Suche: `sk-` trifft dann mitten
im Wort — in `task-scheduler`, `ask-2026`, `risk-and-reward`. Weil der Scan fail-closed ist,
blockiert so ein Fehlalarm die Veröffentlichung einer völlig sauberen Datenbank. An einer
realen 53-MB-Wissensdatenbank gemessen: 33 Treffer, ausnahmslos gewöhnliche Wörter, null
echte Zugangsdaten. Ein Zugangsdatum beginnt ein Token; es setzt nie ein Wort fort — der
Anker kostet also keine Erkennung, sondern nur die Fehlalarme. Belegt durch Tests in beide
Richtungen (`test_vendor_prefixes_do_not_match_inside_ordinary_words` und
`…_still_match_a_real_key_in_context`).

## ADR-004: Integrität ist nicht Authentizität

SHA-256 und Manifest erkennen Übertragungsfehler. Ein feindlicher Transport benötigt
zusätzlich Signaturen oder einen authentifizierten Kanal.

## ADR-006: Publish-Replica als zweite Betriebsart, nicht als Ersatz

`push`/`pull` gleichen zwei Datenbanken **einander an**: ein fremder Snapshot wird per
`MergePolicy` in lokale Zeilen eingerechnet. Das setzt voraus, dass beide Seiten sich über
eine Konfliktregel einig sind. Für den Fall „ich will die Daten des anderen **lesen**, aber
nichts von ihm in meinen Bestand einrechnen" gibt es dafür keine sinnvolle Policy — jede
Wahl wäre eine erfundene fachliche Entscheidung (ADR-002/ADR-003).

Deshalb `publish`/`import-replica`: derselbe geprüfte Snapshot-Weg, aber der Import
**materialisiert eine eigene Datenbank neben der lokalen** (`replica_root/<knoten>/<namespace>.sqlite`)
und fasst den lokalen Bestand nicht an. Die lokale Datenbank wird beim Import nicht einmal
geöffnet. Damit ist die Betriebsart additiv: bestehende Konfigurationen und Merge-Semantik
bleiben unverändert.

**Die Replica wird schreibgeschützt abgelegt.** Sie ist eine Kopie fremder Daten; wer dort
schreibt, verliert seine Änderung beim nächsten Import lautlos. Das Dateiattribut verhindert
den Unfall, nicht den entschlossenen Prozess — Konsumenten sollten zusätzlich mit
`file:...?mode=ro` öffnen.

## ADR-007: Verschlüsselung dort, wo der Transport mitliest

Der typische Transport ist ein synchronisierter Cloud-Ordner. Er ist nicht feindlich, aber er
ist auch nicht privat: Inhalte werden repliziert, indiziert, gesichert und liegen bei einem
Dritten. ADR-004 benennt die Lücke — SHA-256 erkennt Zufall, nicht Absicht.

`publish` verschlüsselt deshalb mit **Fernet** (AES-CBC + HMAC). Das schließt zwei Dinge: Der
Transport sieht keinen Klartext mehr, und eine nachträgliche Änderung am Snapshot fällt auf,
**auch wenn der Angreifer den Manifest-Hash mitfälscht** — der HMAC hängt am Schlüssel, nicht
an der Datei.

**Was es nicht leistet:** Fernet authentifiziert den *Schlüssel*, nicht den *Absender*. Jeder
Schlüsselinhaber kann einen gültigen Snapshot veröffentlichen. Ein Knoten-Identitätsnachweis
bleibt offen (siehe TODO: Signaturadapter). Genau deshalb wird eine importierte Replica
getrennt gehalten und nie gemergt: unbestätigte Herkunft darf den eigenen Bestand nicht
verändern.

**Der Schlüssel darf den Transport nicht berühren.** Ein Schlüssel im Transitverzeichnis ist
ein Widerspruch in sich und wird hart abgelehnt; ein Schlüssel in einem erkennbar
synchronisierten Ordner (OneDrive, Dropbox, …) ebenfalls — abschaltbar über
`allow_key_in_synced_folder`, falls die Erkennung danebenliegt. Dieselbe Regel gilt für
`replica_root`: eine entschlüsselte Replica im Transit würde im Klartext weiterverteilt.

**Der Cipher ist eine optionale Abhängigkeit.** `push`/`pull` kommt weiter ohne
Fremdpakete aus; nur dieser Modus braucht `cryptography` (`pip install
'sqlite-transit-sync[crypto]'`).

## ADR-008: Nutzlast als kuratierter SQL-Dump statt als Binärkopie

Der Replica-Transport überträgt kein SQLite-Dateiabbild, sondern SQL-Text (gzip, dann
verschlüsselt). Gründe: Text ist über SQLite-Builds und Seitengrößen hinweg portabel, und er
komprimiert stark — eine reale 53,6-MB-Datenbank wird zu 11,0 MB Transit.

`sqlite3.iterdump()` allein genügt dafür **nicht**: Enthält die Datenbank eine
FTS-Volltexttabelle, erzeugt es einen Dump, der sich nicht wieder einspielen lässt. Es
schreibt die virtuelle Tabelle über `PRAGMA writable_schema=ON` direkt in `sqlite_master`,
ohne dass der Schema-Cache neu geladen wird — der folgende Insert scheitert mit
`no such table`. Zusätzlich gibt es die Schattentabellen aus, die `CREATE VIRTUAL TABLE`
selbst anlegt.

Der Dump wird deshalb kuratiert: `writable_schema`-Pragmas, `sqlite_master`-Inserts und
Schattentabellen fallen weg, die echte `CREATE VIRTUAL TABLE`-DDL kommt hinein, und der Index
wird nach dem Import mit `rebuild` neu erzeugt. Das ist nicht nur korrekter, sondern deutlich
kleiner: bei der gemessenen Datenbank waren 35.370 von 49.636 Dump-Anweisungen reine
Index-Interna.

**Grenze:** Ein Index ohne eigenen Inhalt (`content=''`) lässt sich nicht rekonstruieren, weil
es nichts gibt, woraus `rebuild` lesen könnte. Solche Tabellen werden im Manifest unter
`contentless_fts` gemeldet, statt stillschweigend leer anzukommen.

