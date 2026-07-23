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

    def __post_init__(self) -> None:
        self.database = Path(self.database).expanduser().resolve()
        self.transit = Path(self.transit).expanduser().resolve()
        self.state = Path(self.state).expanduser().resolve()
        self.node_id = _safe_identifier(self.node_id, "node")
        self.namespace = _safe_identifier(self.namespace, "default")
        self.timestamp_columns = tuple(self.timestamp_columns)
        self.exclude_tables = tuple(self.exclude_tables)
        self.snapshot_exclude_tables = tuple(self.snapshot_exclude_tables)
        if self.database == self.transit or self.transit in self.database.parents:
            raise ValueError("The live database must not be inside the transit directory")

    @classmethod
    def from_file(cls, path: str | Path) -> "SyncConfig":
        config_path = Path(path).expanduser().resolve()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
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
        partial_path.unlink(missing_ok=True)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
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

        try:
            redacted_tables = self._redact_snapshot(partial_path)
            self._quick_check(partial_path)
            digest = _sha256(partial_path)
            size = partial_path.stat().st_size
            os.replace(partial_path, final_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
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
        except Exception:
            final_path.unlink(missing_ok=True)
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
