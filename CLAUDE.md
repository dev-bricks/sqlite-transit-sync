---
name: "sqlite-transit-sync"
type: project-docs
profile: "STANDARD"
version: 0.1.0
created: "2026-07-11"
updated: "2026-07-11"
reason_last_change: "Neutrale Extraktion aus BACH-ProSync"
last_verified: "2026-07-11"
author: "Lukas Geiger / ellmos / BACH Contributors"
anthropic_compatible: true
description: |
  Agent instructions for the standalone sqlite-transit-sync module.
---

# CLAUDE.md

## Projekt

Nutzerneutrales Python-Modul für lokalen SQLite-Abgleich über geprüfte
Transit-Snapshots und anpassbare Merge-Policies.

## Einstieg

1. `START.md`, `STATE.md` und `TODO.md` lesen.
2. Aktive `LOCK*.txt` beachten.
3. `python -m unittest discover -s tests -v` vor und nach Änderungen ausführen.

## Harte Regeln

- Lebende SQLite-, WAL- oder SHM-Dateien niemals in einen Transportordner legen.
- Keine Benutzerpfade, Hostnamen, Datenbanken oder Credentials einbauen.
- Kein automatisches Erstkopieren einer fremden DB und keine Schema-Migration raten.
- Merge-State erst nach erfolgreichem Commit fortschreiben.
- Änderungen an Merge-Semantik in `DECISIONS.md` dokumentieren.
- Deutsche Dokumente verwenden echte Umlaute.

