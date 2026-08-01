# Architecture

## Two operating modes

| | `push` / `pull` | `publish` / `import-replica` |
|---|---|---|
| Direction | two-way convergence | one-way distribution |
| Effect on the local database | rows are merged in | untouched, never opened |
| Result | one converged database | a separate read-only replica per source node |
| Payload | SQLite file | curated SQL dump, gzip, Fernet-encrypted |
| Needs agreement on | a merge policy | only a shared key |
| Extra dependency | none | `cryptography` |

Both use the same publication gate: journal closing, table redaction, credential scan,
`quick_check`, sidecar checks, manifest and SHA-256.

## Data flow — merge mode

```text
local DB A --SQLite backup--> snapshot + manifest --transport--> local staging/read
     ^                                                               |
     |                         MergePolicy                            v
     +----------------------- local transaction <--------------- local DB B
```

Only snapshots cross node boundaries. Before publication, every backup is
switched to SQLite's `DELETE` journal mode and checked for adjacent sidecars.
A live database, WAL, SHM, rollback-journal or other unmanifested SQLite file is
never opened through or left in the transport.

## Data flow — replica mode

```text
local DB A --backup--> snapshot --curated SQL dump--> gzip --Fernet--> transport
                                                                          |
                                                                          v
                          replica_root/<node A>/<namespace>.sqlite  <-- decrypt, restore,
                          (read-only, outside the transport)             rebuild FTS

local DB B  ....................... untouched .......................
```

The key travels out of band and must live outside the transport; the same applies to
`replica_root`, since an imported replica holds decrypted foreign data.

## Components

- `SyncConfig`: resolves paths and defines node, namespace, timestamps and exclusions.
- `TransitSync`: publishes, discovers, verifies, pulls and records local state.
- `Snapshot`: immutable reference to a database snapshot and its manifest.
- `MergePolicy`: application extension point.
- `TimestampMergePolicy`: safe generic baseline for timestamped rows with primary keys.
- `ReplicaTransit`: adds `publish`, `available` and `import_replica` on the same safety gate.
- `ReplicaSnapshot` / `ReplicaImport`: results of publication and import.
- `cli.py`: JSON interface for humans, agents and automations.

## Default merge semantics

The policy intersects tables and columns from local and remote schemas. For each
common table it requires a local primary key and the first configured timestamp
column. A missing local key is inserted; an existing key is updated only if the
remote timestamp is newer. Other rows remain unchanged. Excluded tables and tables
without sufficient semantics are reported as skipped. Equal timestamps use the
lexicographically larger canonical row representation as a deterministic tie-breaker,
so two nodes converge instead of retaining different values.

This deliberately avoids guessing deletion, schema migration or conflict intent.
Applications can provide a custom `MergePolicy` for those decisions.

## Trust boundary

Manifests and SHA-256 protect against partial transfer and accidental corruption.
They do not establish sender identity. Deployments with an untrusted transport must
add signatures or an authenticated transport before accepting snapshots.

Replica mode narrows this: Fernet's HMAC also detects deliberate modification, including
the case where the manifest hash was recomputed to match. What it still does not provide is
*sender identity* — any holder of the shared key can publish a valid snapshot. This is why
an imported replica stays a separate database and is never merged: content of unproven
origin must not change local rows.

## Relationship to BACH ProSync

BACH remains the production integration and owns BACH-specific table semantics,
startup/exit hooks, heartbeat, retention and secret handling. This module is the
neutral reusable core. A future BACH adapter can replace duplicated generic mechanics
only after compatibility and migration tests.
