# Changelog

## [Unreleased]

- **Credential scan before publication (`scan_snapshot_for_secrets`, default on).** Snapshots are
  now checked for credential-shaped values in their *content* before they reach the transit
  directory; a match aborts the push with a `SyncError` naming `table.column`, never the value.
  Complements `snapshot_exclude_tables`, which can only drop a table you already know about —
  the scan catches credentials pasted into free-text columns (notes, logs, session summaries).
  Patterns are vendor-prefixed (OpenAI, Anthropic, OpenRouter, GitHub, GitLab, Google, Slack,
  npm, AWS, PEM private-key blocks); checksums, UUIDs and git SHAs deliberately do **not** match.
  New config keys: `scan_snapshot_for_secrets`, `secret_scan_extra_patterns`,
  `secret_scan_skip_tables`. Rationale in `DECISIONS.md` (ADR-005).

- **Dokumentation & Hygiene**: `llms.txt` Verifikations-Timestamp (2026-07-26) aktualisiert, Testsuite-Verifizierung durchgeführt (8/8 Pytest passed).

## 0.1.1 — 2026-07-25

- Added root `llms.txt` for AI agent discovery, architecture context, and search index.
- Enhanced `README.md` & `README_de.md` with Shields.io badges, language switcher, and Mermaid sequence diagrams.
- Added `pythonpath = "."` to `[tool.pytest.ini_options]` in `pyproject.toml` for seamless test execution.
- Expanded GitHub topics and metadata URLs for improved discoverability.


## 0.1.0 — 2026-07-11

- Neutrale Extraktion aus BACH ProSync.
- SQLite-Backup-Snapshots, Manifest, SHA-256 und Integritätsprüfung.
- Primärschlüssel-basierter Timestamp-Merge mit Schema-Drift-Toleranz.
- Anpassbare Merge-Policy, Tabellen-Ausschlüsse und Snapshot-Redaktion.
- Python-API, JSON-CLI und eigenständige Tests.
- Englische und deutsche Vergleichstabellen zu Distributed SQL einschließlich
  Vor-/Nachteilen, Use Cases und Entscheidungshilfe ergänzt.
