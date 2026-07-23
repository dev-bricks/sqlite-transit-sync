# Security

## Supported use

Use the module only with local live databases and a transport whose participants are
trusted. Keep node state outside the shared transport. Restrict filesystem access to
database, state and transit paths.

## Required application review

Before deployment, define:

1. every table that must be excluded from merge;
2. every table whose rows must be removed from published snapshots;
3. timestamp and clock policy;
4. deletion/tombstone behavior;
5. transport authentication and retention;
6. database migrations and rollback.

The default `snapshot_exclude_tables = ["secrets"]` is only a safety baseline. It
cannot identify application-specific private or regulated data.

When a listed table exists, sqlite-transit-sync deletes its rows from the
snapshot and runs `VACUUM` before publishing. This reduces residual bytes in the
snapshot file, but it is not a substitute for a complete application-specific
redaction list.

## Reporting

Do not include live databases, snapshots, credentials or personal records in a bug
report. Provide a minimal synthetic database and redacted manifest instead.
