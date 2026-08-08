# Security

## Supported use

Use the module only with local live databases and a transport whose participants are
trusted. Keep node state outside the shared transport. Restrict filesystem access to
database, state and transit paths.

`cleanup` verifies each managed snapshot and manifest before selecting it. It is a dry-run
and local-node-only operation by default. Treat `--apply --all-nodes` as administrative
authority over other publishers' artifacts, and review the JSON plan before granting it.

## Required application review

Before deployment, define:

1. every table that must be excluded from merge;
2. every table whose rows must be removed from published snapshots;
3. timestamp and clock policy;
4. deletion/tombstone behavior;
5. transport authentication and retention;
6. database migrations and rollback.

Since 0.2.0 a second, independent guard runs before publication:
`scan_snapshot_for_secrets` (default **on**) inspects snapshot *content* and aborts the push
when a credential-shaped value is found, naming `table.column` but never the value. It exists
because the table rule below can only drop tables you already anticipated, while credentials
in practice end up pasted into free-text columns. It is a safety net, not a guarantee:
patterns are vendor-prefixed, so a bespoke or unprefixed secret still passes. Treat a clean
scan as "no known pattern matched", never as "this snapshot is free of secrets". Disable it
only with `scan_snapshot_for_secrets = false`, and prefer `secret_scan_skip_tables` when a
single table produces false positives.

The default `snapshot_exclude_tables = ["secrets"]` is only a safety baseline. It
cannot identify application-specific private or regulated data.

When a listed table exists, sqlite-transit-sync deletes its rows from the
snapshot and runs `VACUUM` before publishing. This reduces residual bytes in the
snapshot file, but it is not a substitute for a complete application-specific
redaction list.

## Reporting

Do not include live databases, snapshots, credentials or personal records in a bug
report. Provide a minimal synthetic database and redacted manifest instead.
