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

## ADR-010: Zwei Betriebsarten sind dauerhaft redundant, nicht Übergang

Republica ist **keine Übergangslösung, bis der direkte Weg steht**. Das Zielbild sind zwei
Betriebsarten, die nebeneinander bestehen bleiben:

| | Direkter Abgleich (`push`/`pull`) | Republica (`republica-*`) |
|---|---|---|
| Weg | Tunnel zwischen Maschinen (SSH, Tailscale, LAN) | irgendeine Datei-Austauschfläche |
| Richtung | beidseitig, konvergierend | einseitig, Schaufenster |
| Setzt voraus | beide Hosts erreichbar, Trust-Setup, Merge-Regel | ein Schlüssel, einmalig übergeben |
| Ergebnis | eine zusammengeführte Datenbank | je Quellknoten eine eigene Lesekopie |
| Latenz | Minuten | so schnell wie die Austauschfläche |

**Fällt eine aus, trägt die andere.** Der Tunnel fällt aus, wenn ein Host schläft, das Netz
feindlich ist, ein Schlüssel rotiert oder jemand am VPN schraubt. Republica fällt aus, wenn
die Austauschfläche klemmt. Beides passiert, aber selten gleichzeitig — genau das ist der
Nutzen. Eine Fallback-Ebene, die man „erstmal" aufsetzt und dann verrotten lässt, ist am Tag
des Ausfalls wertlos; sie muss also **mitgepflegt und mitgetestet** werden, auch wenn der
direkte Weg gerade problemlos läuft.

Der Preis dieser Redundanz ist **ein einziger Schlüsseltransfer** über einen Kanal, der nicht
der Transportweg selbst ist: ein bestehender Tunnel, ein Passwortmanager, ein USB-Stick, ein
vorgelesener Code. Danach genügt ein geteilter Ordner — dauerhaft und selbst dann, wenn man
diesem Ordner nicht vertraut.

## ADR-009: Sealed Envelope — Zugangsdaten als Datei, niemals in einer Datenbank

Dasselbe Kanal-und-Schlüssel-Paar transportiert auf Wunsch eine **einzelne Datei**
(`envelope-send`/`envelope-receive`). Der Anlass ist das Henne-Ei-Problem: Zwei Maschinen
teilen noch keinen sicheren Kanal, und genau deshalb muss ein Zugangsdatum hinüber.

Zwei Regeln des Schaufensters sind hier **bewusst umgekehrt**:

1. **Der Credential-Scan gilt nicht.** Er soll verhindern, dass ein Zugangsdatum versehentlich
   mitreist. Hier ist es die Fracht — ein Scan würde genau das blockieren, was befördert werden
   soll.
2. **Der Klartext landet als Datei, nie in einer Datenbank.** Ein Zugangsdatum in einer
   Datenbank wird von jedem Backup, jedem Index und jeder Synchronisierung weiterkopiert, die
   sie berührt. Deshalb schreibt der Empfang in ein Verzeichnis (üblicherweise den
   Zugangsdaten-Ordner) mit engen Rechten. In Notizen gehört nur der **Fundort**.

Absicherungen, die daraus folgen: Das Zielverzeichnis darf weder der Transit noch ein
erkennbar synchronisierter Ordner sein — sonst läge das entschlüsselte Geheimnis wieder offen.
Der Umschlag wird nach dem Empfang standardmäßig **aus dem Transit entfernt**, damit ein
Geheimnis nicht dauerhaft im geteilten Ordner liegen bleibt. Der Dateiname wird beim Senden
**und beim Empfangen erneut** entschärft: Der Empfänger darf einem Namen nicht trauen, der
durch einen fremden Ordner gelaufen ist, sonst schriebe ein manipuliertes Manifest außerhalb
des Zielverzeichnisses.

**Abgrenzung:** Das ist ein Kurier, kein Passwort-Manager und keine Dateisynchronisierung.
Für viele oder große Dateien ist es das falsche Werkzeug.

## ADR-006: Republica als zweite Betriebsart, nicht als Ersatz

`push`/`pull` gleichen zwei Datenbanken **einander an**: ein fremder Snapshot wird per
`MergePolicy` in lokale Zeilen eingerechnet. Das setzt voraus, dass beide Seiten sich über
eine Konfliktregel einig sind. Für den Fall „ich will die Daten des anderen **lesen**, aber
nichts von ihm in meinen Bestand einrechnen" gibt es dafür keine sinnvolle Policy — jede
Wahl wäre eine erfundene fachliche Entscheidung (ADR-002/ADR-003).

Deshalb `republica-publish`/`republica-import`: derselbe geprüfte Snapshot-Weg, aber der Import
**materialisiert eine eigene Datenbank neben der lokalen** (`republica_root/<knoten>/<namespace>.sqlite`)
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
`republica_root`: eine entschlüsselte Replica im Transit würde im Klartext weiterverteilt.

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

## ADR-011: Snapshot-Bereinigung braucht getrennte Auswahl- und Freigabegates

Aufbewahrung ist eine administrative Entscheidung, kein Nebeneffekt von `push` oder `sync`.
`cleanup` plant daher standardmäßig nur: Es prüft Manifest, Dateigröße, SHA-256 und
`PRAGMA quick_check`, wählt Snapshots nur dann aus, wenn sie sowohl älter als die
Aufbewahrungsfrist als auch außerhalb der neuesten Mindestanzahl liegen, und löscht erst mit
ausdrücklichem `--apply`.

Der Standardbereich ist der eigene Knoten. Fremde Snapshots können für andere Teilnehmer die
einzige noch verfügbare Kopie sein; ihre Verwaltung erfordert deshalb zusätzlich
`--all-nodes`. Die Parameter bleiben Sache der integrierenden Anwendung. Republica-Artefakte
sind ein anderes Format mit eigener Vertrauens- und Aufbewahrungsgrenze und werden von diesem
Mechanismus nicht berührt.

## ADR-012: Lebenszyklus-Adapter dürfen einen geprüften Pending-Ausschnitt wählen

Der neutrale Standard `pull()` verarbeitet weiterhin alle ausstehenden Snapshots. Manche
Anwendungen besitzen jedoch bewusst eine andere Lebenszyklus-Policy, etwa „pro Start nur den
neuesten zulässigen Fremdstand“. Ohne Carrier-Schnittstelle müsste ein solcher Adapter Merge,
Transaktion und State-Fortschreibung kopieren; er wäre damit kein dünner Adapter mehr.

`pull_selected()` akzeptiert deshalb ausschließlich Snapshot-Basisnamen, die `pending()` aktuell
liefert. Pfade, Manifestnamen, Duplikate und nicht mehr ausstehende Namen werden vor jeder
Änderung abgelehnt. Der ausgewählte Snapshot durchläuft anschließend exakt dieselbe Verifikation,
Merge-Transaktion und State-Fortschreibung wie `pull()`. Die Auswahl ist Anwendungspolitik; die
Datenmechanik bleibt beim Carrier.
