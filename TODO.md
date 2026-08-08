# TODO

- [ ] Optionalen Signaturadapter für nicht vollständig vertrauenswürdige Transporte entwerfen.
      Der Replica-Modus verschlüsselt bereits (ADR-007), belegt damit aber nur den Besitz des
      gemeinsamen Schlüssels. Offen bleibt die Knoten-Identität — erst damit wäre ein Merge
      fremder Daten vertretbar (heute bewusst getrennte Replica).
- [ ] Retention für Replica-Snapshots: alte `*.republica` je Knoten im Transit aufräumen,
      ohne fremde Snapshots unkontrolliert zu löschen (hängt am selben Retention-Punkt unten).
- [ ] Inhaltslose FTS-Indizes (`content=''`) im Replica-Modus: heute nur im Manifest gemeldet,
      nicht rekonstruierbar. Falls ein Anwendungsfall auftaucht, Schattentabellen mitführen.
- [ ] Referenzadapter für Tombstone-basierte Löschsynchronisation ergänzen.
- [ ] BACH-Kompatibilitätsadapter erst nach goldenem Vergleichstest evaluieren.
- [x] Retention für direkte Transit-Snapshots ergänzt: verifizierendes `cleanup`, Dry-Run
      als Standard, lokaler Knoten als Standardbereich und fremde Knoten nur nach
      ausdrücklicher Freigabe. Republica-Retention bleibt separat offen.
- [x] Paket- und Release-Gates vor einer öffentlichen Veröffentlichung durchführen.

---

## STATUS

| Category | Status | Notes |
|----------|--------|-------|
| Secrets | :green_circle: | No secrets in tracked files (final_gate_check.py) |
| Private Data (PII) | :green_circle: | No PII patterns found |
| .gitignore | :green_circle: | Minimum entries present |
| Language (English) | :green_circle: | README.md in English; README_de.md as companion |
| BACH Internals | :green_circle: | No BACH-internal files |
| Database Files | :green_circle: | No .db files tracked |
| README.md | :green_circle: | Present, English |
| LICENSE | :green_circle: | MIT |
| **Overall** | **READY** | |

**Audit Date:** 2026-07-23
**Gate Check Exit Code:** `0`
