"""Verified snapshot transit and row-level merging for local SQLite databases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence


PROTOCOL_VERSION = 1
SNAPSHOT_SUFFIX = ".sqlite-snapshot"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_TIMESTAMP_COLUMNS = ("updated_at", "modified_at", "created_at")
_DEFAULT_EXCLUDE_TABLES = ("secrets", "sqlite_sequence")

# Credential trigger patterns live in data, not in code, so detection can be
# tightened over time without a release: see `credential-triggers.json` next to
# this module and the `secret_patterns_file` config key.
#
# `snapshot_exclude_tables` drops a *table* you already know about. This scan
# answers the different question the table rule cannot: is there a credential
# somewhere in free-text content — a note, a log line, a session summary?
_TRIGGER_FILE = Path(__file__).with_name("credential-triggers.json")


@dataclass(frozen=True, slots=True)
class SecretPattern:
    """One credential trigger: label, matcher and optional SQL pre-filter."""

    name: str
    regex: "re.Pattern[str]"
    prefilter: str | None = None


def load_secret_patterns(path: str | Path | None = None) -> tuple[SecretPattern, ...]:
    """Load credential triggers from JSON (defaults to the bundled file)."""
    source = Path(path).expanduser() if path else _TRIGGER_FILE
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SyncError(f"Invalid credential trigger file: {source}: {error}") from error
    entries = raw.get("patterns", []) if isinstance(raw, dict) else raw
    patterns: list[SecretPattern] = []
    for index, entry in enumerate(entries):
        try:
            name = str(entry["name"])
            compiled = re.compile(str(entry["regex"]))
        except (KeyError, TypeError, re.error) as error:
            raise SyncError(
                f"Invalid credential trigger #{index} in {source}: {error}"
            ) from error
        prefilter = entry.get("prefilter") or None
        patterns.append(SecretPattern(name, compiled, prefilter))
    return tuple(patterns)


class SyncError(RuntimeError):
    """Raised when a snapshot or synchronization operation is unsafe."""


def _safe_identifier(value: str, fallback: str) -> str:
    cleaned = _SAFE_ID.sub("-", value.strip()).strip("-.")
    if not cleaned:
        cleaned = fallback
    if "__" in cleaned:
        cleaned = cleaned.replace("__", "-")
    return cleaned[:80]


def _utc_token() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _sqlite_artifact_paths(path: Path) -> tuple[Path, ...]:
    sidecars = tuple(sorted(path.parent.glob(f"{path.name}-*")))
    return (path, *sidecars)


def _remove_sqlite_artifacts(path: Path) -> None:
    failures: list[tuple[Path, OSError]] = []
    for candidate in _sqlite_artifact_paths(path):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            failures.append((candidate, error))
    if failures:
        details = "; ".join(f"{candidate}: {error}" for candidate, error in failures)
        raise SyncError(f"Could not clean temporary SQLite artifacts: {details}")


def _assert_no_sqlite_sidecars(path: Path) -> None:
    leftovers = _sqlite_artifact_paths(path)[1:]
    if leftovers:
        names = ", ".join(candidate.name for candidate in leftovers)
        raise SyncError(f"Unclosed SQLite snapshot has sidecars: {names}")


def _remove_manifest_artifacts(path: Path) -> None:
    candidates = (path, *sorted(path.parent.glob(f".{path.name}.*.tmp")))
    failures: list[tuple[Path, OSError]] = []
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            failures.append((candidate, error))
    if failures:
        details = "; ".join(f"{candidate}: {error}" for candidate, error in failures)
        raise SyncError(f"Could not clean snapshot manifest artifacts: {details}")


@dataclass(slots=True)
class SyncConfig:
    """Portable configuration for one node participating in a transit."""

    database: Path
    transit: Path
    state: Path
    node_id: str = field(default_factory=socket.gethostname)
    namespace: str = "default"
    timestamp_columns: tuple[str, ...] = _DEFAULT_TIMESTAMP_COLUMNS
    exclude_tables: tuple[str, ...] = _DEFAULT_EXCLUDE_TABLES
    snapshot_exclude_tables: tuple[str, ...] = ("secrets",)
    scan_snapshot_for_secrets: bool = True
    secret_scan_extra_patterns: tuple[str, ...] = ()
    secret_scan_skip_tables: tuple[str, ...] = ()
    secret_patterns_file: Path | None = None
    # Publish-replica mode only (see replica.py). Unused by push/pull, so an existing
    # configuration keeps working untouched.
    key_file: Path | None = None
    replica_root: Path | None = None
    allow_key_in_synced_folder: bool = False

    def __post_init__(self) -> None:
        self.database = Path(self.database).expanduser().resolve()
        self.transit = Path(self.transit).expanduser().resolve()
        self.state = Path(self.state).expanduser().resolve()
        self.node_id = _safe_identifier(self.node_id, "node")
        self.namespace = _safe_identifier(self.namespace, "default")
        self.timestamp_columns = tuple(self.timestamp_columns)
        self.exclude_tables = tuple(self.exclude_tables)
        self.snapshot_exclude_tables = tuple(self.snapshot_exclude_tables)
        self.secret_scan_extra_patterns = tuple(self.secret_scan_extra_patterns)
        self.secret_scan_skip_tables = tuple(self.secret_scan_skip_tables)
        if self.secret_patterns_file is not None:
            self.secret_patterns_file = Path(self.secret_patterns_file).expanduser().resolve()
        # The key path may come from the environment so that a configuration file can
        # be shared or version-controlled without naming a local credential location.
        if self.key_file is None:
            from_env = os.environ.get("SQLITE_TRANSIT_SYNC_KEY_FILE")
            if from_env:
                self.key_file = Path(from_env)
        if self.key_file is not None:
            self.key_file = Path(self.key_file).expanduser().resolve()
        self.replica_root = (
            Path(self.replica_root).expanduser().resolve()
            if self.replica_root is not None
            else Path.home() / ".transit-replicas"
        )
        if self.database == self.transit or self.transit in self.database.parents:
            raise ValueError("The live database must not be inside the transit directory")
        # An imported replica holds decrypted foreign data. Inside the transit it would
        # be republished in the clear — the exact outcome the encryption prevents.
        if self.replica_root == self.transit or self.transit in self.replica_root.parents:
            raise ValueError("Replicas must not be stored inside the transit directory")

    @classmethod
    def from_file(cls, path: str | Path) -> "SyncConfig":
        config_path = Path(path).expanduser().resolve()
        return cls.from_bytes(config_path.read_bytes(), source_path=config_path)

    @classmethod
    def from_bytes(cls, payload: bytes, *, source_path: str | Path) -> "SyncConfig":
        """Parse exact config bytes using paths relative to their source file."""
        config_path = Path(source_path).expanduser().resolve()
        raw = json.loads(payload.decode("utf-8"))
        base = config_path.parent

        def resolve(value: str) -> Path:
            candidate = Path(value).expanduser()
            return candidate if candidate.is_absolute() else base / candidate

        database = resolve(raw["database"])
        transit = resolve(raw["transit"])
        state = resolve(raw.get("state", ".sync-state.json"))
        return cls(
            database=database,
            transit=transit,
            state=state,
            node_id=raw.get("node_id") or socket.gethostname(),
            namespace=raw.get("namespace", "default"),
            timestamp_columns=tuple(raw.get("timestamp_columns", _DEFAULT_TIMESTAMP_COLUMNS)),
            exclude_tables=tuple(raw.get("exclude_tables", _DEFAULT_EXCLUDE_TABLES)),
            snapshot_exclude_tables=tuple(raw.get("snapshot_exclude_tables", ("secrets",))),
            scan_snapshot_for_secrets=bool(raw.get("scan_snapshot_for_secrets", True)),
            secret_scan_extra_patterns=tuple(raw.get("secret_scan_extra_patterns", ())),
            secret_scan_skip_tables=tuple(raw.get("secret_scan_skip_tables", ())),
            secret_patterns_file=(
                resolve(raw["secret_patterns_file"])
                if raw.get("secret_patterns_file")
                else None
            ),
            key_file=resolve(raw["key_file"]) if raw.get("key_file") else None,
            replica_root=resolve(raw["replica_root"]) if raw.get("replica_root") else None,
            allow_key_in_synced_folder=bool(raw.get("allow_key_in_synced_folder", False)),
        )

    def write(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        payload = {
            "database": str(self.database),
            "transit": str(self.transit),
            "state": str(self.state),
            "node_id": self.node_id,
            "namespace": self.namespace,
            "timestamp_columns": list(self.timestamp_columns),
            "exclude_tables": list(self.exclude_tables),
            "snapshot_exclude_tables": list(self.snapshot_exclude_tables),
            "scan_snapshot_for_secrets": self.scan_snapshot_for_secrets,
            "secret_scan_extra_patterns": list(self.secret_scan_extra_patterns),
            "secret_scan_skip_tables": list(self.secret_scan_skip_tables),
            "secret_patterns_file": (
                str(self.secret_patterns_file) if self.secret_patterns_file else None
            ),
            "key_file": str(self.key_file) if self.key_file else None,
            "replica_root": str(self.replica_root) if self.replica_root else None,
            "allow_key_in_synced_folder": self.allow_key_in_synced_folder,
        }
        _atomic_json_write(target, payload)
        return target


@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    manifest_path: Path
    node_id: str
    namespace: str
    created_at: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["manifest_path"] = str(self.manifest_path)
        return value


@dataclass(slots=True)
class MergeReport:
    snapshot: str
    source_node: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_tables: list[str] = field(default_factory=list)
    tables: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MergePolicy(Protocol):
    """Interface for application-specific snapshot merge policies."""

    def merge(
        self,
        local: sqlite3.Connection,
        remote: sqlite3.Connection,
        snapshot: Snapshot,
    ) -> MergeReport:
        ...


class TimestampMergePolicy:
    """Last-write-wins per primary key for tables with a timestamp column.

    Tables without a primary key or a configured timestamp column are ignored.
    Deletions are intentionally not inferred. Applications needing tombstones,
    CRDTs, domain validation, or clock-skew handling should supply another policy.
    """

    def __init__(
        self,
        timestamp_columns: Sequence[str] = _DEFAULT_TIMESTAMP_COLUMNS,
        exclude_tables: Sequence[str] = _DEFAULT_EXCLUDE_TABLES,
    ) -> None:
        self.timestamp_columns = tuple(timestamp_columns)
        self.exclude_tables = set(exclude_tables)

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def _table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
        return conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()

    @staticmethod
    def _remote_wins(
        remote_value: Any,
        local_value: Any,
        remote_fingerprint: str,
        local_fingerprint: str,
    ) -> bool:
        if remote_value is None:
            return False
        if local_value is None:
            return True
        try:
            if remote_value != local_value:
                return remote_value > local_value
        except TypeError:
            if str(remote_value) != str(local_value):
                return str(remote_value) > str(local_value)
        return remote_fingerprint > local_fingerprint

    def merge(
        self,
        local: sqlite3.Connection,
        remote: sqlite3.Connection,
        snapshot: Snapshot,
    ) -> MergeReport:
        local.row_factory = sqlite3.Row
        remote.row_factory = sqlite3.Row
        report = MergeReport(snapshot=snapshot.path.name, source_node=snapshot.node_id)
        common_tables = sorted(self._tables(local) & self._tables(remote))

        for table in common_tables:
            if table in self.exclude_tables:
                report.skipped_tables.append(table)
                continue
            local_info = self._table_info(local, table)
            remote_info = self._table_info(remote, table)
            local_columns = [row[1] for row in local_info]
            remote_columns = {row[1] for row in remote_info}
            shared = [column for column in local_columns if column in remote_columns]
            primary_keys = [row[1] for row in sorted(local_info, key=lambda row: row[5]) if row[5] > 0]
            timestamp = next((name for name in self.timestamp_columns if name in shared), None)
            if not primary_keys or not timestamp or not all(key in shared for key in primary_keys):
                report.skipped_tables.append(table)
                continue

            table_sql = _quote_identifier(table)
            columns_sql = ", ".join(_quote_identifier(column) for column in shared)
            placeholders = ", ".join("?" for _ in shared)
            where = " AND ".join(f"{_quote_identifier(key)} = ?" for key in primary_keys)
            update_columns = [column for column in shared if column not in primary_keys]
            update_sql = ", ".join(f"{_quote_identifier(column)} = ?" for column in update_columns)
            table_stats = {"inserted": 0, "updated": 0, "unchanged": 0}

            for remote_row in remote.execute(f"SELECT {columns_sql} FROM {table_sql}"):
                key_values = tuple(remote_row[key] for key in primary_keys)
                local_row = local.execute(
                    f"SELECT {columns_sql} FROM {table_sql} WHERE {where}", key_values
                ).fetchone()
                if local_row is None:
                    local.execute(
                        f"INSERT INTO {table_sql} ({columns_sql}) VALUES ({placeholders})",
                        tuple(remote_row[column] for column in shared),
                    )
                    table_stats["inserted"] += 1
                else:
                    remote_fingerprint = json.dumps(
                        [remote_row[column] for column in shared],
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                    local_fingerprint = json.dumps(
                        [local_row[column] for column in shared],
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    )
                if local_row is not None and self._remote_wins(
                    remote_row[timestamp],
                    local_row[timestamp],
                    remote_fingerprint,
                    local_fingerprint,
                ):
                    if update_columns:
                        local.execute(
                            f"UPDATE {table_sql} SET {update_sql} WHERE {where}",
                            tuple(remote_row[column] for column in update_columns) + key_values,
                        )
                    table_stats["updated"] += 1
                elif local_row is not None:
                    table_stats["unchanged"] += 1

            report.tables[table] = table_stats
            report.inserted += table_stats["inserted"]
            report.updated += table_stats["updated"]
            report.unchanged += table_stats["unchanged"]

        return report


class TransitSync:
    """Coordinates verified snapshots between independent local SQLite writers."""

    def __init__(self, config: SyncConfig, policy: MergePolicy | None = None) -> None:
        self.config = config
        self.policy = policy or TimestampMergePolicy(
            timestamp_columns=config.timestamp_columns,
            exclude_tables=config.exclude_tables,
        )
        self.config.transit.mkdir(parents=True, exist_ok=True)
        self.config.state.parent.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict[str, Any]:
        if not self.config.state.exists():
            return {"protocol": PROTOCOL_VERSION, "last_pulled": {}}
        try:
            state = json.loads(self.config.state.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SyncError(f"Invalid sync state: {self.config.state}: {error}") from error
        state.setdefault("last_pulled", {})
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        state["protocol"] = PROTOCOL_VERSION
        _atomic_json_write(self.config.state, state)

    def _snapshot_name(self, created_at: str) -> str:
        return (
            f"{self.config.namespace}__{self.config.node_id}__{created_at}"
            f"{SNAPSHOT_SUFFIX}"
        )

    def _redact_snapshot(self, path: Path) -> list[str]:
        if not self.config.snapshot_exclude_tables:
            return []
        connection = sqlite3.connect(str(path))
        redacted: list[str] = []
        try:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in self.config.snapshot_exclude_tables:
                if table in existing:
                    connection.execute(f"DELETE FROM {_quote_identifier(table)}")
                    redacted.append(table)
            connection.commit()
            if redacted:
                connection.execute("VACUUM")
        finally:
            connection.close()
        return redacted

    @staticmethod
    def _close_snapshot(path: Path) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(path))
            result = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        except sqlite3.Error as error:
            raise SyncError(f"Could not close SQLite snapshot {path}: {error}") from error
        finally:
            if connection is not None:
                connection.close()
        if not result or str(result[0]).lower() != "delete":
            raise SyncError(f"Could not switch SQLite snapshot to DELETE journal mode: {path}")
        _assert_no_sqlite_sidecars(path)

    def _scan_snapshot_for_secrets(self, path: Path) -> list[str]:
        """Report credential-looking values in snapshot content.

        Runs on the snapshot copy, never on the live database. Returns locations
        as ``table.column`` — deliberately **without** the matched value, so the
        credential is not copied into logs, exceptions or CI output.
        """
        if not self.config.scan_snapshot_for_secrets:
            return []

        patterns = list(load_secret_patterns(self.config.secret_patterns_file))
        for index, expr in enumerate(self.config.secret_scan_extra_patterns):
            try:
                patterns.append(SecretPattern(f"custom[{index}]", re.compile(expr)))
            except re.error as error:
                raise SyncError(f"Invalid secret_scan_extra_patterns[{index}]: {error}") from error
        if not patterns:
            return []

        # Rows are pre-filtered in SQL when every pattern carries a literal that
        # must be present. A single pattern without one makes the pre-filter
        # unsound — missing a credential is worse than a slower scan — so the
        # column is then read in full.
        prefilters = [p.prefilter for p in patterns]
        can_prefilter = all(prefilters)
        skip = set(self.config.secret_scan_skip_tables)
        findings: list[str] = []
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            connection.text_factory = lambda value: value.decode("utf-8", "replace")
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not row[0].startswith("sqlite_") and row[0] not in skip
            ]
            for table in tables:
                columns = [
                    row[1]
                    for row in connection.execute(
                        f"PRAGMA table_info({_quote_identifier(table)})"
                    ).fetchall()
                    if (row[2] or "").upper() not in {"INTEGER", "REAL", "NUMERIC", "BOOLEAN"}
                ]
                for column in columns:
                    quoted = _quote_identifier(column)
                    select = (
                        f"SELECT CAST({quoted} AS TEXT) FROM {_quote_identifier(table)}"
                    )
                    params: list[str] = []
                    if can_prefilter:
                        clause = " OR ".join(f"CAST({quoted} AS TEXT) LIKE ?" for _ in prefilters)
                        select = f"{select} WHERE {clause}"
                        params = [f"%{token}%" for token in prefilters]
                    try:
                        rows = connection.execute(select, params).fetchall()
                    except sqlite3.Error:
                        continue  # virtual tables / odd column types stay out of the way
                    for (value,) in rows:
                        if not value:
                            continue
                        for pattern in patterns:
                            if pattern.regex.search(value):
                                location = f"{table}.{column} ({pattern.name})"
                                if location not in findings:
                                    findings.append(location)
                                break
        finally:
            connection.close()
        return findings

    @staticmethod
    def _quick_check(path: Path) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            result = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as error:
            raise SyncError(f"SQLite verification failed for {path}: {error}") from error
        finally:
            if connection is not None:
                connection.close()
        if not result or result[0] != "ok":
            raise SyncError(f"SQLite quick_check failed for {path}: {result}")

    def push(self) -> Snapshot:
        if not self.config.database.is_file():
            raise SyncError(f"Live database not found: {self.config.database}")
        created_at = _utc_token()
        final_path = self.config.transit / self._snapshot_name(created_at)
        partial_path = final_path.with_name(f".{final_path.name}.partial")
        manifest_path = final_path.with_suffix(final_path.suffix + ".json")
        _remove_sqlite_artifacts(partial_path)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            try:
                source = sqlite3.connect(str(self.config.database))
                destination = sqlite3.connect(str(partial_path))
                with destination:
                    source.backup(destination)
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
        except Exception as error:
            try:
                _remove_sqlite_artifacts(partial_path)
            except SyncError as cleanup_error:
                raise cleanup_error from error
            raise

        published_snapshot = False
        try:
            self._close_snapshot(partial_path)
            redacted_tables = self._redact_snapshot(partial_path)
            leaks = self._scan_snapshot_for_secrets(partial_path)
            if leaks:
                raise SyncError(
                    "Snapshot publication aborted: credential-looking values remain in "
                    + ", ".join(leaks)
                    + ". Remove them from the source database, add the table to "
                    "snapshot_exclude_tables, or disable scan_snapshot_for_secrets if "
                    "this is a false positive. The value itself is not shown on purpose."
                )
            self._quick_check(partial_path)
            _assert_no_sqlite_sidecars(partial_path)
            digest = _sha256(partial_path)
            size = partial_path.stat().st_size
            os.replace(partial_path, final_path)
            published_snapshot = True
            _assert_no_sqlite_sidecars(final_path)
        except Exception as error:
            try:
                _remove_sqlite_artifacts(partial_path)
                if published_snapshot:
                    _remove_sqlite_artifacts(final_path)
            except SyncError as cleanup_error:
                raise cleanup_error from error
            raise
        manifest = {
            "protocol": PROTOCOL_VERSION,
            "namespace": self.config.namespace,
            "node_id": self.config.node_id,
            "created_at": created_at,
            "snapshot": final_path.name,
            "sha256": digest,
            "size": size,
            "redacted_tables": redacted_tables,
        }
        try:
            _atomic_json_write(manifest_path, manifest)
        except Exception as error:
            try:
                _remove_manifest_artifacts(manifest_path)
                _remove_sqlite_artifacts(final_path)
                _remove_sqlite_artifacts(partial_path)
            except SyncError as cleanup_error:
                raise cleanup_error from error
            raise
        return Snapshot(
            path=final_path,
            manifest_path=manifest_path,
            node_id=self.config.node_id,
            namespace=self.config.namespace,
            created_at=created_at,
            sha256=digest,
            size=size,
        )

    def _read_snapshot(self, manifest_path: Path, verify: bool = True) -> Snapshot:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SyncError(f"Invalid manifest {manifest_path}: {error}") from error
        required = {"protocol", "namespace", "node_id", "created_at", "snapshot", "sha256", "size"}
        if not required.issubset(manifest):
            raise SyncError(f"Incomplete manifest: {manifest_path}")
        if manifest["protocol"] != PROTOCOL_VERSION:
            raise SyncError(f"Unsupported protocol in {manifest_path}")
        if manifest["namespace"] != self.config.namespace:
            raise SyncError(f"Wrong namespace in {manifest_path}")
        snapshot_path = manifest_path.parent / manifest["snapshot"]
        if snapshot_path.parent.resolve() != self.config.transit.resolve():
            raise SyncError(f"Snapshot escapes transit directory: {snapshot_path}")
        snapshot = Snapshot(
            path=snapshot_path,
            manifest_path=manifest_path,
            node_id=_safe_identifier(manifest["node_id"], "node"),
            namespace=manifest["namespace"],
            created_at=manifest["created_at"],
            sha256=manifest["sha256"],
            size=int(manifest["size"]),
        )
        if verify:
            if not snapshot.path.is_file():
                raise SyncError(f"Snapshot missing: {snapshot.path}")
            if snapshot.path.stat().st_size != snapshot.size or _sha256(snapshot.path) != snapshot.sha256:
                raise SyncError(f"Snapshot checksum mismatch: {snapshot.path}")
            self._quick_check(snapshot.path)
        return snapshot

    def snapshots(self, verify: bool = False) -> list[Snapshot]:
        manifests = self.config.transit.glob(
            f"{self.config.namespace}__*__*{SNAPSHOT_SUFFIX}.json"
        )
        snapshots: list[Snapshot] = []
        for manifest in manifests:
            snapshots.append(self._read_snapshot(manifest, verify=verify))
        return sorted(snapshots, key=lambda item: (item.created_at, item.node_id))

    def pending(self, verify: bool = False) -> list[Snapshot]:
        state = self._load_state()
        last_pulled = state.get("last_pulled", {})
        return [
            snapshot
            for snapshot in self.snapshots(verify=verify)
            if snapshot.node_id != self.config.node_id
            and snapshot.created_at > last_pulled.get(snapshot.node_id, "")
        ]

    def pull(self, dry_run: bool = False) -> list[MergeReport]:
        pending = self.pending(verify=True)
        if dry_run:
            return [MergeReport(snapshot=item.path.name, source_node=item.node_id) for item in pending]
        if not self.config.database.is_file():
            raise SyncError(
                "The local database must exist with its application schema before pull; "
                "automatic first-copy is intentionally disabled"
            )

        reports: list[MergeReport] = []
        state = self._load_state()
        for snapshot in pending:
            local: sqlite3.Connection | None = None
            remote: sqlite3.Connection | None = None
            try:
                local = sqlite3.connect(str(self.config.database))
                remote = sqlite3.connect(f"file:{snapshot.path.as_posix()}?mode=ro", uri=True)
                local.execute("BEGIN IMMEDIATE")
                report = self.policy.merge(local, remote, snapshot)
                local.commit()
            except Exception:
                if local is not None:
                    local.rollback()
                raise
            finally:
                if remote is not None:
                    remote.close()
                if local is not None:
                    local.close()
            state.setdefault("last_pulled", {})[snapshot.node_id] = snapshot.created_at
            self._save_state(state)
            reports.append(report)
        return reports

    def sync(self) -> dict[str, Any]:
        pulled = self.pull()
        pushed = self.push()
        return {"pulled": [report.as_dict() for report in pulled], "pushed": pushed.as_dict()}

    def verify(self) -> dict[str, Any]:
        self._quick_check(self.config.database)
        snapshots = self.snapshots(verify=True)
        return {"database": "ok", "verified_snapshots": len(snapshots)}

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        pending = self.pending(verify=False)
        return {
            "node_id": self.config.node_id,
            "namespace": self.config.namespace,
            "database": str(self.config.database),
            "transit": str(self.config.transit),
            "state": str(self.config.state),
            "database_exists": self.config.database.is_file(),
            "pending": [snapshot.as_dict() for snapshot in pending],
            "last_pulled": state.get("last_pulled", {}),
        }
