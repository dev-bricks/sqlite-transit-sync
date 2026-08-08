# Changelog

## Unreleased

- **Selected pending pull:** new `TransitSync.pull_selected()` lets lifecycle adapters process
  an explicit verified pending subset while reusing the carrier's merge transaction and state
  advancement. Paths, manifest names, duplicates and non-pending names fail before mutation.

- **Conservative direct-snapshot cleanup:** new `TransitSync.cleanup()` and CLI
  `cleanup` select artifacts by age and per-node minimum retention. The operation verifies
  manifest, SHA-256 and SQLite integrity first, is a dry-run by default, manages only the
  local node by default, and requires both `--all-nodes` and `--apply` before deleting
  foreign-node snapshot/manifest pairs.

- **Discoverability:** corrected the canonical repository owner from `dev-bricks` to
  `ellmos-ai` in package metadata, `llms.txt`, release documentation, and README
  license badges so external package and GitHub links resolve to the live repository.

- **The one-way mode is now called Republica** (subtitle: "the showcase method"). Each node
  puts an encrypted showcase of its database into a shared file area; everyone can look,
  nobody can change it. Renamed throughout: module `republica.py`, `RepublicaTransit`,
  config key `republica_root`, default path `~/.republica`, snapshot suffix `.republica`,
  and CLI commands `republica-publish` / `republica-list` / `republica-import`. Not on PyPI
  yet, so no compatibility shim is carried — the previous names existed for hours.
- **Documented as a permanent fallback layer, not a migration step** (ADR-010). Direct sync
  over a tunnel and Republica over a file area are meant to run side by side, with a failure
  table showing which mode still carries when the other stalls: host asleep, VPN down, key
  rotation pending, shared folder broken. A fallback left to rot is worthless on the day it
  is needed. Setup cost is stated plainly: one key transfer over any channel that is not the
  transport itself.
- **Sealed envelope: `envelope-send` / `envelope-receive`** (ADR-009). One encrypted file over
  the same channel and key, for the bootstrap case where two machines share no secure channel
  yet and a credential must cross. Two showcase rules are deliberately inverted: the credential
  scan does not apply (it would block the payload being moved), and the plaintext is written
  **as a file** with mode `0600` and never into a database. Envelopes are removed from the
  transit after receipt; unsealing into the transit or a cloud-synced folder is refused; the
  filename is re-sanitised on arrival so a crafted manifest cannot escape the target directory.

- **Republica mode (`publish` / `republica-import`), additive to `push`/`pull`.**
  Distributes a database one way instead of converging two: the import materialises a
  separate read-only replica per source node under `republica_root` and never touches the
  local database. Payload is a curated SQL dump, gzip-compressed and Fernet-encrypted, so a
  synchronised cloud folder no longer carries readable content. New config keys `key_file`,
  `republica_root` and `allow_key_in_synced_folder`; new CLI commands `keygen`, `publish`,
  `republica-list` and `republica-import`. See ADR-006 to ADR-008.
- **Restore FTS databases correctly.** A plain `iterdump` cannot be replayed when the
  database contains a full-text index: it writes the virtual table through
  `PRAGMA writable_schema` without a schema reload, then re-emits the shadow tables that
  `CREATE VIRTUAL TABLE` creates itself. The dump is now curated and the index rebuilt after
  import — which also removed 35370 of 49636 statements on a real database.
- **Fix credential-scan false positives that blocked publication.** Vendor prefixes had no
  left anchor, so `sk-` matched inside ordinary words — `task-scheduler`, `ask-2026`,
  `risk-and-reward`. Because the scan is fail-closed, this stopped a clean 53 MB database
  from being published: 33 hits, all words, no credential. All prefixes are now anchored
  with `(?<![A-Za-z0-9])`; triggers file bumped to version 2.
- **Metadata tests compare sources instead of a literal.** `test_version_consistency` and
  `test_llms_txt_version_parity` asserted the string `0.2.0`, so every release required a
  test edit and a partial bump would still pass — the one failure the tests exist to catch.
  They now compare `__version__`, `pyproject.toml` and `llms.txt` against each other.
- Optional dependency `crypto` (`cryptography`). The core stays dependency free: only
  replica mode encrypts.

- **Dokumentation, SEO & Discoverability**: `llms.txt` Verifikations-Timestamp auf 2026-08-01 aktualisiert, Shields.io Badges für Ecosystem (`dev-bricks`) und Umbrella (`open-bricks`) in `README.md` & `README_de.md` integriert, 26/26 Pytest-Tests verifiziert [G 2026-08-01].
- Close every backup into `DELETE` journal mode before publication and remove the
  complete temporary SQLite artifact family on backup, redaction, credential-scan,
  verification, or manifest failure. WAL source databases can no longer leave
  unmanifested `-wal`, `-shm`, `-journal`, or similar sidecars in transit.
- Add `SyncConfig.from_bytes(payload, source_path=...)` so callers can hash and
  parse the exact same config bytes. `from_file()` delegates to this parser and
  keeps identical source-relative path semantics.

## 0.2.0 — 2026-07-29

- **Dokumentation, SEO & Discoverability**: `llms.txt` Verifikations-Timestamp auf 2026-07-30 aktualisiert, GFM LLM Note Callout (`> [!NOTE]`) in `README.md` & `README_de.md` integriert, 19/19 Pytest-Tests verifiziert [G 2026-07-30].
- **CI/CD & Automation**: Added `.github/workflows/ci.yml` for automated GitHub Actions testing on Python 3.10, 3.11, 3.12, 3.13.
- **Tests & Metadata**: Added `tests/test_metadata.py` checking version parity, exports, and `llms.txt` integrity (19/19 passed).
- **Dokumentation & Hygiene**: `llms.txt` Last-checked Datum auf 2026-07-29 und Version auf 0.2.0 angeglichen, Badges in `README.md` & `README_de.md` auf 19/19 grüne Pytest-Tests aktualisiert [G 2026-07-29].
- **Credential scan before publication (`scan_snapshot_for_secrets`, default on).** Snapshots are
  now checked for credential-shaped values in their *content* before they reach the transit
  directory; a match aborts the push with a `SyncError` naming `table.column`, never the value.
  Complements `snapshot_exclude_tables`, which can only drop a table you already know about —
  the scan catches credentials pasted into free-text columns (notes, logs, session summaries).
  Patterns are vendor-prefixed (OpenAI, Anthropic, OpenRouter, GitHub, GitLab, Google, Slack,
  npm, AWS, PEM private-key blocks); checksums, UUIDs and git SHAs deliberately do **not** match.
  New config keys: `scan_snapshot_for_secrets`, `secret_scan_extra_patterns`,
  `secret_scan_skip_tables`, `secret_patterns_file`. Rationale in `DECISIONS.md` (ADR-005).
- **Triggers are data, not code.** Patterns now live in
  `sqlite_transit_sync/credential-triggers.json` and can be replaced via
  `secret_patterns_file`, so detection can be tightened over time without a release.
  Entries carry an optional `prefilter` literal for the SQL pre-filter; a pattern without
  one falls back to a full column read rather than silently missing rows.
- **Documented the escape hatch and the alternatives.** README (en/de) now lists every
  config key with its default, states that disabling the scan is a legitimate choice when
  you control the transit yourself, and points at real secret-distribution tools
  (Vaultwarden, SOPS+age, pass, KeePassXC over Syncthing, Infisical/OpenBao, platform
  keystores) so users stop trying to make a sync yard do that job.
- Added `[tool.setuptools.package-data]` so the trigger file reaches installed wheels.
- Version 0.2.0; `__version__` had drifted at 0.1.0 and now tracks `pyproject.toml`.

- **Dokumentation, SEO & Hygiene**: `llms.txt` Verifikations-Timestamp auf 2026-07-27 aktualisiert, Shields.io Badges in `README.md` & `README_de.md` auf 16/16 grüne Pytest-Tests angehoben, 100% grün verifiziert [G 2026-07-27].
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
