"""Republica — the showcase method: encrypted one-way database showcases.

Each node puts an encrypted *showcase* of its database into a shared file area;
every other node can look at it, none can change it. Hence the name: a re-publication
of a database, readable only by whoever holds the key.

This is an additive operating mode next to :class:`~sqlite_transit_sync.core.TransitSync`.
The difference is intent, not plumbing:

``push``/``pull``       two-way convergence. A remote snapshot is *merged into* the
                        local database by a :class:`MergePolicy`.
``republica-publish``/  one-way distribution. A remote showcase is *materialised beside*
``republica-import``    the local database as a separate read-only copy and never
                        touches local rows.

Republica exists for the case where nodes want to *read* each other's data without
agreeing on a merge semantic — and where the transport (a synchronised cloud folder,
a share, a USB disk) must not see plaintext.

**It is a permanent fallback layer, not a stopgap.** Two operating modes are meant to
coexist: a direct tunnel between machines (fast, converging, needs reachable hosts and a
trust setup) and Republica over any file area (slow, one-way, needs almost nothing).
When one fails — a host is offline, a tunnel is down, a key rotation is pending, a
network is hostile — the other still carries. Setting Republica up "for now" and letting
it rot defeats the point; the whole value is that it works on the day the other path
does not.

**Setup cost is one key transfer.** The shared key must reach the other machines through
some channel that is not the transport itself: an existing encrypted tunnel, a password
manager, a USB stick, a phone read-out. Once. After that a plain shared folder — even an
untrusted one — is enough forever.

Besides databases, the same channel and key carry a :func:`sealed envelope
<RepublicaTransit.envelope_send>`: a single encrypted file that arrives as a file and
never enters a database (ADR-009).

Payload pipeline
----------------

``SQLite backup API -> curated SQL dump -> gzip -> Fernet``

Every stage earns its place:

* the **backup API** produces a consistent snapshot of a database that is being
  written to, which a plain file copy or a dump of the live file cannot;
* the **curated dump** (see :func:`_curated_dump`) is portable across SQLite
  builds and page sizes, unlike the binary file;
* **gzip** matters because SQL text is highly redundant — a real 53.6 MB database
  compresses to 8.2 MB;
* **Fernet** provides authenticated encryption. Manifest SHA-256 detects accidental
  corruption; it does not stop a deliberate rewrite. Fernet's HMAC does.

Security boundary
-----------------

Fernet authenticates *the key*, not *the sender*: any holder of the shared key can
publish a well-formed snapshot. That is why an imported replica is deliberately kept
as a separate database and never merged. Establishing per-node identity is the job
of a later trust gate, not of this module.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .core import (
    PROTOCOL_VERSION,
    Snapshot,
    SyncConfig,
    SyncError,
    TransitSync,
    _atomic_json_write,
    _remove_manifest_artifacts,
    _remove_sqlite_artifacts,
    _safe_identifier,
    _sha256,
    _utc_token,
)

REPUBLICA_SUFFIX = ".republica"
REPUBLICA_PROTOCOL_VERSION = 1
ENVELOPE_SUFFIX = ".envelope"
ENVELOPE_PROTOCOL_VERSION = 1
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

# Shadow tables of FTS3/4/5 virtual tables. They are an implementation detail of
# the index and are rebuilt on import, so they never travel: on a real database
# they were 35370 of 49636 dump statements — the index, not the data.
_FTS_SHADOW_SUFFIXES = (
    "_data",
    "_idx",
    "_content",
    "_docsize",
    "_config",
    "_segments",
    "_segdir",
    "_stat",
)

_STATEMENT_TARGET = re.compile(
    r"""\s*(?:CREATE\s+TABLE|CREATE\s+VIRTUAL\s+TABLE|INSERT\s+INTO)\s+"""
    r"""(?:IF\s+NOT\s+EXISTS\s+)?["'\[`]?([A-Za-z0-9_]+)""",
    re.IGNORECASE,
)
_SQLITE_MASTER_INSERT = re.compile(r"\s*INSERT\s+INTO\s+sqlite_master", re.IGNORECASE)
_FTS_CONTENT_OPTION = re.compile(r"content\s*=\s*'([^']*)'|content\s*=\s*([A-Za-z0-9_]+)", re.IGNORECASE)

# Folders whose whole purpose is to copy their contents elsewhere. A key in one of
# them is a key already handed to the transport it is supposed to protect.
_SYNCED_FOLDER_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "nextcloud")


@dataclass(frozen=True, slots=True)
class RepublicaSnapshot:
    """A published, encrypted snapshot and its plaintext manifest."""

    path: Path
    manifest_path: Path
    node_id: str
    namespace: str
    created_at: str
    sha256: str
    size: int
    payload_sha256: str
    payload_size: int

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["manifest_path"] = str(self.manifest_path)
        return value


@dataclass(frozen=True, slots=True)
class Envelope:
    """A sealed single file waiting in the transit."""

    path: Path
    manifest_path: Path
    node_id: str
    filename: str
    label: str
    created_at: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["path"] = str(self.path)
        value["manifest_path"] = str(self.manifest_path)
        return value


@dataclass(slots=True)
class EnvelopeReceipt:
    """Result of unsealing one envelope into a local directory."""

    source_node: str
    filename: str
    label: str
    created_at: str
    written_to: str
    size: int
    removed_from_transit: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RepublicaImport:
    """Result of materialising one remote snapshot as a local replica."""

    source_node: str
    namespace: str
    created_at: str
    replica_path: str
    tables: int
    rows: int
    rebuilt_indexes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fts_tables(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Map FTS virtual tables to their content mode.

    ``external`` means the index mirrors a real table (``content=items``) and can be
    regenerated with ``rebuild``. A contentless index (``content=''``) stores nothing
    to rebuild *from*, so its rows have to travel — see :func:`_curated_dump`.
    """
    tables: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE '%USING fts%'"
    ).fetchall()
    for name, sql in rows:
        match = _FTS_CONTENT_OPTION.search(sql or "")
        source = (match.group(1) or match.group(2) or "") if match else None
        tables[name] = {
            "sql": sql,
            "external": bool(source),
            "contentless": match is not None and source == "",
        }
    return tables


def _curated_dump(connection: sqlite3.Connection) -> Iterator[str]:
    """Yield a restorable SQL dump, with FTS internals replaced by a rebuild.

    ``sqlite3.iterdump`` cannot restore a database containing an FTS virtual table:
    it emits the virtual table by writing into ``sqlite_master`` under
    ``PRAGMA writable_schema=ON``, and the schema cache is not reloaded before the
    following inserts, so a replay fails with ``no such table``. It then also emits
    the shadow tables that ``CREATE VIRTUAL TABLE`` creates by itself.

    So three classes of statement are dropped and replaced by honest DDL plus a
    rebuild: the ``writable_schema`` pragmas, the ``sqlite_master`` inserts, and the
    shadow tables. Rows of an external-content index are dropped too — they are a
    copy of a table that travels anyway. Python 3.13's ``iterdump(filter=...)``
    would only cover table selection, not this rewrite.
    """
    fts = _fts_tables(connection)
    shadows = {f"{table}{suffix}" for table in fts for suffix in _FTS_SHADOW_SUFFIXES}

    for statement in connection.iterdump():
        stripped = statement.strip()
        if stripped.upper().startswith("PRAGMA WRITABLE_SCHEMA"):
            continue
        if _SQLITE_MASTER_INSERT.match(stripped):
            continue
        if stripped == "COMMIT;":
            continue  # re-emitted last, after the rebuilt indexes
        match = _STATEMENT_TARGET.match(stripped)
        target = match.group(1) if match else None
        if target in shadows:
            continue
        if target in fts and fts[target]["external"]:
            continue
        yield statement

    for name, info in fts.items():
        yield f"{info['sql']};"
    for name, info in fts.items():
        if info["external"]:
            yield f'INSERT INTO "{name}"("{name}") VALUES(\'rebuild\');'
    yield "COMMIT;"


def _load_fernet(key_file: Path | None):
    """Return a Fernet instance, or fail with an actionable message.

    ``cryptography`` is an optional dependency: the core module stays dependency
    free, and only this mode needs a cipher.
    """
    try:
        from cryptography.fernet import Fernet
    except ModuleNotFoundError as error:  # pragma: no cover - depends on environment
        raise SyncError(
            "Publish-replica mode needs the 'cryptography' package. "
            "Install it with: pip install 'sqlite-transit-sync[crypto]'"
        ) from error

    if key_file is None:
        raise SyncError(
            "No encryption key configured. Set 'key_file' in the node configuration "
            "or the SQLITE_TRANSIT_SYNC_KEY_FILE environment variable. Generate one "
            "with: python -m sqlite_transit_sync keygen --key-file <path>"
        )
    if not key_file.is_file():
        raise SyncError(f"Encryption key file not found: {key_file}")
    material = key_file.read_bytes().strip()
    if not material:
        raise SyncError(f"Encryption key file is empty: {key_file}")
    try:
        return Fernet(material)
    except (ValueError, TypeError) as error:
        raise SyncError(
            f"Invalid Fernet key in {key_file}: expected 32 url-safe base64-encoded bytes"
        ) from error


def generate_key() -> bytes:
    """Return a fresh Fernet key. Distribute it out of band, never through the transit."""
    try:
        from cryptography.fernet import Fernet
    except ModuleNotFoundError as error:  # pragma: no cover - depends on environment
        raise SyncError(
            "Key generation needs the 'cryptography' package. "
            "Install it with: pip install 'sqlite-transit-sync[crypto]'"
        ) from error
    return Fernet.generate_key()


class RepublicaTransit(TransitSync):
    """Publish encrypted snapshots and import foreign ones as read-only replicas.

    Inherits the snapshot safety apparatus of :class:`TransitSync` — journal
    closing, table redaction, credential scan, ``quick_check``, sidecar checks — so
    a published replica is held to exactly the same publication bar as a merge
    snapshot, plus encryption.
    """

    def __init__(self, config: SyncConfig, policy: Any | None = None) -> None:
        super().__init__(config, policy)
        self._assert_key_outside_transport()

    # -- paths -----------------------------------------------------------

    @property
    def republica_root(self) -> Path:
        return self.config.republica_root

    def _own_transit(self) -> Path:
        """Publication target: one directory per node, so hosts never interleave."""
        return self.config.transit / self.config.node_id

    def _republica_path(self, source_node: str) -> Path:
        return self.republica_root / source_node / f"{self.config.namespace}.sqlite"

    def _assert_key_outside_transport(self) -> None:
        """A key inside the transport protects nothing — fail before publishing.

        The transit check is a logical certainty. The cloud-folder check is a
        heuristic on the path name and can be waived per configuration for a
        deployment that knows better.
        """
        key_file = self.config.key_file
        if key_file is None:
            return
        transit = self.config.transit
        if key_file == transit or transit in key_file.parents:
            raise SyncError(
                f"The encryption key must not live inside the transit directory: {key_file}"
            )
        if self.config.allow_key_in_synced_folder:
            return
        if self._looks_synced(key_file):
            raise SyncError(
                f"The encryption key appears to be inside a synchronised folder: "
                f"{key_file}. Keep it on local disk only, or set "
                "'allow_key_in_synced_folder' if this detection is wrong."
            )

    # -- publish ---------------------------------------------------------

    def publish(self) -> RepublicaSnapshot:
        """Publish a consistent, redacted, scanned, encrypted snapshot."""
        if not self.config.database.is_file():
            raise SyncError(f"Live database not found: {self.config.database}")
        cipher = _load_fernet(self.config.key_file)

        created_at = _utc_token()
        target_dir = self._own_transit()
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / (
            f"{self.config.namespace}__{self.config.node_id}__{created_at}{REPUBLICA_SUFFIX}"
        )
        manifest_path = final_path.with_suffix(final_path.suffix + ".json")

        staging = Path(tempfile.mkdtemp(prefix=".replica-", dir=target_dir))
        snapshot_db = staging / "snapshot.db"
        payload_gz = staging / "payload.sql.gz"
        try:
            self._backup_to(snapshot_db)
            self._close_snapshot(snapshot_db)
            redacted = self._redact_snapshot(snapshot_db)
            leaks = self._scan_snapshot_for_secrets(snapshot_db)
            if leaks:
                raise SyncError(
                    "Replica publication aborted: credential-looking values remain in "
                    + ", ".join(leaks)
                    + ". Remove them from the source database, add the table to "
                    "snapshot_exclude_tables, or disable scan_snapshot_for_secrets if "
                    "this is a false positive. The value itself is not shown on purpose."
                )
            self._quick_check(snapshot_db)

            statements, tables, contentless = self._write_payload(snapshot_db, payload_gz)
            payload = payload_gz.read_bytes()
            payload_sha256 = _sha256(payload_gz)
            payload_size = payload_gz.stat().st_size

            token = cipher.encrypt(payload)
            partial = final_path.with_name(f".{final_path.name}.partial")
            partial.write_bytes(token)
            digest = _sha256(partial)
            size = partial.stat().st_size
            os.replace(partial, final_path)
        except Exception:
            self._cleanup(staging, final_path, manifest_path)
            raise
        finally:
            self._remove_tree(staging)

        manifest = {
            "protocol": PROTOCOL_VERSION,
            "republica_protocol": REPUBLICA_PROTOCOL_VERSION,
            "mode": "replica",
            "namespace": self.config.namespace,
            "node_id": self.config.node_id,
            "created_at": created_at,
            "snapshot": final_path.name,
            # Basename only: a manifest travels through the transport and must not
            # leak local directory layout or account names.
            "source_database": self.config.database.name,
            "encryption": "fernet",
            "compression": "gzip",
            "payload": "sql-dump",
            "sha256": digest,
            "size": size,
            "payload_sha256": payload_sha256,
            "payload_size": payload_size,
            "statements": statements,
            "tables": tables,
            "redacted_tables": redacted,
            "contentless_fts": contentless,
        }
        try:
            _atomic_json_write(manifest_path, manifest)
        except Exception:
            self._cleanup(None, final_path, manifest_path)
            raise

        return RepublicaSnapshot(
            path=final_path,
            manifest_path=manifest_path,
            node_id=self.config.node_id,
            namespace=self.config.namespace,
            created_at=created_at,
            sha256=digest,
            size=size,
            payload_sha256=payload_sha256,
            payload_size=payload_size,
        )

    def _backup_to(self, target: Path) -> None:
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(str(self.config.database))
            destination = sqlite3.connect(str(target))
            with destination:
                source.backup(destination)
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

    def _write_payload(self, snapshot_db: Path, payload_gz: Path) -> tuple[int, int, list[str]]:
        connection = sqlite3.connect(f"file:{snapshot_db.as_posix()}?mode=ro", uri=True)
        try:
            fts = _fts_tables(connection)
            contentless = sorted(name for name, info in fts.items() if info["contentless"])
            tables = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            statements = 0
            with gzip.open(payload_gz, "wt", encoding="utf-8", newline="\n", compresslevel=6) as out:
                for statement in _curated_dump(connection):
                    out.write(statement + "\n")
                    statements += 1
        finally:
            connection.close()
        return statements, int(tables), contentless

    # -- import ----------------------------------------------------------

    def available(self) -> list[dict[str, Any]]:
        """List foreign replica manifests in the transit, newest per node last."""
        found: list[dict[str, Any]] = []
        if not self.config.transit.is_dir():
            return found
        pattern = f"{self.config.namespace}__*__*{REPUBLICA_SUFFIX}.json"
        for manifest_path in sorted(self.config.transit.glob(f"*/{pattern}")):
            try:
                manifest = self._read_republica_manifest(manifest_path)
            except SyncError:
                continue  # a foreign or damaged manifest must not stop discovery
            if manifest["node_id"] == self.config.node_id:
                continue
            found.append(manifest)
        return sorted(found, key=lambda item: (item["node_id"], item["created_at"]))

    def _read_republica_manifest(self, manifest_path: Path) -> dict[str, Any]:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SyncError(f"Invalid replica manifest {manifest_path}: {error}") from error
        required = {
            "republica_protocol",
            "namespace",
            "node_id",
            "created_at",
            "snapshot",
            "sha256",
            "size",
            "payload_sha256",
        }
        if not required.issubset(manifest):
            raise SyncError(f"Incomplete replica manifest: {manifest_path}")
        if manifest.get("mode") != "replica":
            raise SyncError(f"Not a replica manifest: {manifest_path}")
        if manifest["republica_protocol"] != REPUBLICA_PROTOCOL_VERSION:
            raise SyncError(f"Unsupported replica protocol in {manifest_path}")
        if manifest["namespace"] != self.config.namespace:
            raise SyncError(f"Wrong namespace in {manifest_path}")
        snapshot_path = manifest_path.parent / manifest["snapshot"]
        if snapshot_path.parent.resolve().parent != self.config.transit.resolve():
            raise SyncError(f"Replica escapes transit directory: {snapshot_path}")
        manifest["node_id"] = _safe_identifier(manifest["node_id"], "node")
        manifest["_path"] = str(snapshot_path)
        manifest["_manifest_path"] = str(manifest_path)
        return manifest

    def import_republica(self, node_id: str | None = None) -> list[RepublicaImport]:
        """Materialise foreign snapshots as separate read-only replicas.

        Never merges: each source node gets its own database file under
        ``republica_root``. The local database is not opened at all.
        """
        cipher = _load_fernet(self.config.key_file)
        newest: dict[str, dict[str, Any]] = {}
        for manifest in self.available():
            if node_id and manifest["node_id"] != node_id:
                continue
            current = newest.get(manifest["node_id"])
            if current is None or manifest["created_at"] > current["created_at"]:
                newest[manifest["node_id"]] = manifest

        results: list[RepublicaImport] = []
        for source_node, manifest in sorted(newest.items()):
            results.append(self._import_one(cipher, manifest, source_node))
        return results

    def _import_one(self, cipher: Any, manifest: dict[str, Any], source_node: str) -> RepublicaImport:
        snapshot_path = Path(manifest["_path"])
        if not snapshot_path.is_file():
            raise SyncError(f"Replica snapshot missing: {snapshot_path}")
        if snapshot_path.stat().st_size != int(manifest["size"]):
            raise SyncError(f"Replica size mismatch: {snapshot_path}")
        # Checked before decryption so an accidentally truncated transfer is named
        # as such, instead of surfacing as an opaque cipher error.
        if _sha256(snapshot_path) != manifest["sha256"]:
            raise SyncError(f"Replica checksum mismatch: {snapshot_path}")

        try:
            from cryptography.fernet import InvalidToken
        except ModuleNotFoundError as error:  # pragma: no cover - depends on environment
            raise SyncError("Publish-replica mode needs the 'cryptography' package.") from error
        try:
            payload = cipher.decrypt(snapshot_path.read_bytes())
        except InvalidToken as error:
            raise SyncError(
                f"Replica decryption failed for {snapshot_path.name}: the payload was "
                "modified or was encrypted with a different key. Nothing was imported."
            ) from error

        target = self._republica_path(source_node)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=target.parent))
        building = staging / "replica.db"
        try:
            # End-to-end check of the plaintext. The manifest hash above covers the
            # ciphertext in transit; this one covers the payload the sender actually
            # compressed, and so also catches a mismatch between manifest and content.
            if _sha256_bytes(payload) != manifest["payload_sha256"]:
                raise SyncError(
                    f"Replica payload hash mismatch for {snapshot_path.name}: the "
                    "decrypted content does not match its manifest. Nothing was imported."
                )
            try:
                script = gzip.decompress(payload).decode("utf-8")
            except (OSError, EOFError, UnicodeDecodeError) as error:
                raise SyncError(f"Corrupt replica payload in {snapshot_path.name}: {error}") from error

            connection = sqlite3.connect(str(building))
            try:
                connection.executescript(script)
                connection.commit()
                rebuilt = sorted(name for name, info in _fts_tables(connection).items() if info["external"])
                tables = connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
                rows = _count_rows(connection)
            except sqlite3.Error as error:
                raise SyncError(f"Replica restore failed for {snapshot_path.name}: {error}") from error
            finally:
                connection.close()

            self._quick_check(building)
            _unlock_file(target)
            _remove_sqlite_artifacts(target)
            os.replace(building, target)
            _lock_file(target)
        finally:
            self._remove_tree(staging)

        return RepublicaImport(
            source_node=source_node,
            namespace=self.config.namespace,
            created_at=manifest["created_at"],
            replica_path=str(target),
            tables=int(tables),
            rows=rows,
            rebuilt_indexes=rebuilt,
        )

    # -- sealed envelope -------------------------------------------------
    #
    # Same channel, same key, deliberately different rules. An envelope carries a
    # credential *on purpose*, so two things are inverted compared to a showcase:
    # the credential scan does not apply (it would block the payload it is meant to
    # move), and the plaintext is written as a file and never into a database.

    def envelope_send(
        self, source: Path, *, recipient: str | None = None, label: str | None = None
    ) -> Envelope:
        """Seal one file into the transit.

        Intended for the case where two machines share no other secure channel: the
        file is encrypted here and only ever decrypted on a machine holding the key.
        Keep envelopes small and few — this is a courier, not a file sync.
        """
        cipher = _load_fernet(self.config.key_file)
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise SyncError(f"File to seal not found: {source}")
        transit = self.config.transit
        if source == transit or transit in source.parents:
            raise SyncError(
                f"Refusing to seal a file that already lies in the transit: {source}"
            )

        created_at = _utc_token()
        filename = _safe_filename(source.name)
        tag = _safe_identifier(label or source.stem, "envelope")
        target_dir = self._own_transit()
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / f"{tag}__{self.config.node_id}__{created_at}{ENVELOPE_SUFFIX}"
        manifest_path = final_path.with_suffix(final_path.suffix + ".json")

        payload = source.read_bytes()
        payload_sha256 = _sha256_bytes(payload)
        partial = final_path.with_name(f".{final_path.name}.partial")
        try:
            partial.write_bytes(cipher.encrypt(payload))
            digest = _sha256(partial)
            size = partial.stat().st_size
            os.replace(partial, final_path)
        except Exception:
            partial.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise
        finally:
            del payload

        manifest = {
            "protocol": PROTOCOL_VERSION,
            "envelope_protocol": ENVELOPE_PROTOCOL_VERSION,
            "mode": "envelope",
            "namespace": self.config.namespace,
            "node_id": self.config.node_id,
            "created_at": created_at,
            "envelope": final_path.name,
            # Basename only - a manifest travels in the clear and must not disclose
            # where the secret lives on the sending machine.
            "filename": filename,
            "label": tag,
            "recipient": _safe_identifier(recipient, "any") if recipient else "",
            "encryption": "fernet",
            "sha256": digest,
            "size": size,
            "payload_sha256": payload_sha256,
        }
        try:
            _atomic_json_write(manifest_path, manifest)
        except Exception:
            final_path.unlink(missing_ok=True)
            _remove_manifest_artifacts(manifest_path)
            raise

        return Envelope(
            path=final_path,
            manifest_path=manifest_path,
            node_id=self.config.node_id,
            filename=filename,
            label=tag,
            created_at=created_at,
            sha256=digest,
            size=size,
        )

    def envelopes(self) -> list[dict[str, Any]]:
        """List foreign envelopes waiting in the transit."""
        found: list[dict[str, Any]] = []
        if not self.config.transit.is_dir():
            return found
        for manifest_path in sorted(self.config.transit.glob(f"*/*{ENVELOPE_SUFFIX}.json")):
            try:
                manifest = self._read_envelope_manifest(manifest_path)
            except SyncError:
                continue
            if manifest["node_id"] == self.config.node_id:
                continue
            found.append(manifest)
        return sorted(found, key=lambda item: (item["node_id"], item["created_at"]))

    def _read_envelope_manifest(self, manifest_path: Path) -> dict[str, Any]:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SyncError(f"Invalid envelope manifest {manifest_path}: {error}") from error
        required = {
            "envelope_protocol",
            "node_id",
            "created_at",
            "envelope",
            "filename",
            "sha256",
            "size",
            "payload_sha256",
        }
        if not required.issubset(manifest):
            raise SyncError(f"Incomplete envelope manifest: {manifest_path}")
        if manifest.get("mode") != "envelope":
            raise SyncError(f"Not an envelope manifest: {manifest_path}")
        if manifest["envelope_protocol"] != ENVELOPE_PROTOCOL_VERSION:
            raise SyncError(f"Unsupported envelope protocol in {manifest_path}")
        envelope_path = manifest_path.parent / manifest["envelope"]
        if envelope_path.parent.resolve().parent != self.config.transit.resolve():
            raise SyncError(f"Envelope escapes transit directory: {envelope_path}")
        manifest["node_id"] = _safe_identifier(manifest["node_id"], "node")
        manifest["_path"] = str(envelope_path)
        manifest["_manifest_path"] = str(manifest_path)
        return manifest

    def envelope_receive(
        self, into: Path, *, node_id: str | None = None, keep: bool = False
    ) -> list[EnvelopeReceipt]:
        """Unseal envelopes into a directory, as files.

        The plaintext is written to disk and **never** into a database: a credential
        in a database would be copied onward by every backup, index and sync that
        touches it. Only the location of a secret belongs in notes — the secret itself
        belongs in a file with narrow permissions.

        By default the envelope is removed from the transit afterwards, so a secret
        does not keep lying in a shared folder once it has arrived.
        """
        cipher = _load_fernet(self.config.key_file)
        into = Path(into).expanduser().resolve()
        transit = self.config.transit
        if into == transit or transit in into.parents:
            raise SyncError(
                f"Refusing to unseal into the transit directory: {into}. The decrypted "
                "file would be redistributed in the clear."
            )
        if self._looks_synced(into) and not self.config.allow_key_in_synced_folder:
            raise SyncError(
                f"Refusing to unseal into what looks like a synchronised folder: {into}. "
                "Choose a local directory, or set 'allow_key_in_synced_folder' if this "
                "detection is wrong."
            )
        if into.exists() and not into.is_dir():
            raise SyncError(f"Unseal target is not a directory: {into}")
        into.mkdir(parents=True, exist_ok=True)

        try:
            from cryptography.fernet import InvalidToken
        except ModuleNotFoundError as error:  # pragma: no cover - depends on environment
            raise SyncError("Sealed envelopes need the 'cryptography' package.") from error

        receipts: list[EnvelopeReceipt] = []
        for manifest in self.envelopes():
            if node_id and manifest["node_id"] != node_id:
                continue
            envelope_path = Path(manifest["_path"])
            if not envelope_path.is_file():
                raise SyncError(f"Envelope missing: {envelope_path}")
            if envelope_path.stat().st_size != int(manifest["size"]):
                raise SyncError(f"Envelope size mismatch: {envelope_path.name}")
            if _sha256(envelope_path) != manifest["sha256"]:
                raise SyncError(f"Envelope checksum mismatch: {envelope_path.name}")
            try:
                payload = cipher.decrypt(envelope_path.read_bytes())
            except InvalidToken as error:
                raise SyncError(
                    f"Envelope decryption failed for {envelope_path.name}: it was modified "
                    "or sealed with a different key. Nothing was written."
                ) from error
            if _sha256_bytes(payload) != manifest["payload_sha256"]:
                raise SyncError(
                    f"Envelope payload hash mismatch for {envelope_path.name}. Nothing was written."
                )

            # The filename comes from another machine: re-sanitise it here rather than
            # trusting the sender, so a crafted manifest cannot escape the target folder.
            filename = _safe_filename(Path(manifest["filename"]).name)
            target = into / f"{manifest['node_id']}__{filename}"
            temporary = into / f".{target.name}.partial"
            try:
                temporary.write_bytes(payload)
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            finally:
                del payload

            removed = False
            if not keep:
                try:
                    envelope_path.unlink(missing_ok=True)
                    Path(manifest["_manifest_path"]).unlink(missing_ok=True)
                    removed = True
                except OSError:
                    removed = False

            receipts.append(
                EnvelopeReceipt(
                    source_node=manifest["node_id"],
                    filename=filename,
                    label=manifest.get("label", ""),
                    created_at=manifest["created_at"],
                    written_to=str(target),
                    size=target.stat().st_size,
                    removed_from_transit=removed,
                )
            )
        return receipts

    # -- housekeeping ----------------------------------------------------

    @staticmethod
    def _looks_synced(path: Path) -> bool:
        lowered = str(path).lower()
        return any(marker in lowered for marker in _SYNCED_FOLDER_MARKERS)

    @staticmethod
    def _remove_tree(directory: Path) -> None:
        if not directory.exists():
            return
        for child in sorted(directory.rglob("*"), reverse=True):
            try:
                if child.is_dir():
                    child.rmdir()
                else:
                    _unlock_file(child)
                    child.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

    def _cleanup(self, staging: Path | None, final_path: Path, manifest_path: Path) -> None:
        if staging is not None:
            self._remove_tree(staging)
        try:
            final_path.with_name(f".{final_path.name}.partial").unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            _remove_manifest_artifacts(manifest_path)
        except (OSError, SyncError):
            pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_filename(value: str, fallback: str = "sealed-file") -> str:
    """Reduce a name to a plain filename.

    Applied on send *and* again on receive: the second machine must not trust a
    filename that travelled through a shared folder, or a crafted manifest could
    write outside the target directory.
    """
    cleaned = _SAFE_FILENAME.sub("-", Path(value).name).strip("-. ")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned[:120] or fallback


def _count_rows(connection: sqlite3.Connection) -> int:
    total = 0
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in tables:
        try:
            total += connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error:
            continue  # shadow or virtual tables without a row count
    return total


def _lock_file(path: Path) -> None:
    """Mark the replica read-only on disk, so a stray writer fails loudly.

    Advisory: it stops accidents, not a determined process. Consumers should still
    open the file with ``file:...?mode=ro``.
    """
    try:
        path.chmod(stat.S_IREAD)
    except OSError:
        pass


def _unlock_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
