"""Tests for publish-replica mode: encrypted one-way snapshots."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlite_transit_sync import ReplicaTransit, SyncConfig, SyncError, generate_key

try:
    from cryptography.fernet import Fernet  # noqa: F401

    HAVE_CRYPTO = True
except ModuleNotFoundError:  # pragma: no cover - depends on environment
    HAVE_CRYPTO = False


SCHEMA = """
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE secrets (
    id TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# An external-content FTS5 index plus its trigger: the shape that a plain
# iterdump cannot restore, and the shape both candidate databases use.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE items_fts USING fts5(value, content=items, content_rowid=rowid);
CREATE TRIGGER items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, value) VALUES (new.rowid, new.value);
END;
"""


def create_db(path: Path, *, with_fts: bool = False, rows: int = 3) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        if with_fts:
            connection.executescript(FTS_SCHEMA)
        for index in range(rows):
            connection.execute(
                "INSERT INTO items VALUES (?, ?, ?)",
                (f"row-{index}", f"payload {index} mit echten Umlauten: Grüße", "2026-01-01T00:00:00Z"),
            )
        connection.execute(
            "INSERT INTO secrets VALUES ('token', 'do-not-publish', '2026-01-01T00:00:00Z')"
        )
        connection.commit()
    finally:
        connection.close()


@unittest.skipUnless(HAVE_CRYPTO, "cryptography is required for publish-replica mode")
class ReplicaTransitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transit = self.root / "transit"
        self.key_file = self.root / "keys" / "replica.key"
        self.key_file.parent.mkdir(parents=True)
        self.key_file.write_bytes(generate_key())

        self.a_db = self.root / "a.db"
        self.b_db = self.root / "b.db"
        create_db(self.a_db, with_fts=True)
        create_db(self.b_db, rows=1)

        self.a = ReplicaTransit(self._config(self.a_db, "node-a"))
        self.b = ReplicaTransit(self._config(self.b_db, "node-b"))

    def tearDown(self) -> None:
        # Imported replicas are marked read-only; clear it so cleanup can remove them.
        for path in self.root.rglob("*.sqlite"):
            try:
                path.chmod(0o600)
            except OSError:
                pass
        self.temp.cleanup()

    def _config(self, database: Path, node_id: str) -> SyncConfig:
        return SyncConfig(
            database=database,
            transit=self.transit,
            state=self.root / f"{node_id}-state.json",
            node_id=node_id,
            namespace="memory",
            key_file=self.key_file,
            replica_root=self.root / "replicas",
        )

    # -- roundtrip -------------------------------------------------------

    def test_publish_import_roundtrip_reproduces_content_and_rebuilds_fts(self) -> None:
        snapshot = self.a.publish()
        self.assertTrue(snapshot.path.is_file())
        self.assertTrue(snapshot.manifest_path.is_file())

        imports = self.b.import_replica()
        self.assertEqual(1, len(imports))
        report = imports[0]
        self.assertEqual("node-a", report.source_node)
        self.assertEqual(["items_fts"], report.rebuilt_indexes)

        replica = Path(report.replica_path)
        self.assertTrue(replica.is_file())
        connection = sqlite3.connect(f"file:{replica.as_posix()}?mode=ro", uri=True)
        try:
            values = [row[0] for row in connection.execute("SELECT value FROM items ORDER BY id")]
            matches = connection.execute(
                "SELECT count(*) FROM items_fts WHERE items_fts MATCH 'payload'"
            ).fetchone()[0]
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()

        source = sqlite3.connect(f"file:{self.a_db.as_posix()}?mode=ro", uri=True)
        try:
            expected = [row[0] for row in source.execute("SELECT value FROM items ORDER BY id")]
        finally:
            source.close()

        self.assertEqual(expected, values)
        self.assertIn("Grüße", values[0])  # UTF-8 survives dump, gzip and cipher
        self.assertEqual(3, matches)  # the index was rebuilt, not transported

    def _local_item_count(self) -> int:
        connection = sqlite3.connect(f"file:{self.b_db.as_posix()}?mode=ro", uri=True)
        try:
            return connection.execute("SELECT count(*) FROM items").fetchone()[0]
        finally:
            connection.close()

    def test_replica_is_separate_and_local_database_is_untouched(self) -> None:
        self.a.publish()
        before = self._local_item_count()
        self.b.import_replica()
        after = self._local_item_count()
        self.assertEqual(before, after, "import-replica must never merge into the local database")
        self.assertEqual(1, after)

    def test_publishing_node_does_not_import_its_own_snapshot(self) -> None:
        self.a.publish()
        self.assertEqual([], self.a.available())
        self.assertEqual([], self.a.import_replica())

    def test_import_is_repeatable_and_replaces_the_previous_replica(self) -> None:
        self.a.publish()
        first = self.b.import_replica()[0]
        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute(
                "INSERT INTO items VALUES ('row-new', 'zusätzlich', '2026-02-01T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()
        self.a.publish()
        second = self.b.import_replica()[0]

        self.assertEqual(first.replica_path, second.replica_path)
        connection = sqlite3.connect(f"file:{Path(second.replica_path).as_posix()}?mode=ro", uri=True)
        try:
            self.assertEqual(4, connection.execute("SELECT count(*) FROM items").fetchone()[0])
        finally:
            connection.close()

    # -- confidentiality and tamper resistance ---------------------------

    def test_published_payload_is_not_readable_without_the_key(self) -> None:
        snapshot = self.a.publish()
        raw = snapshot.path.read_bytes()
        self.assertNotIn(b"payload 0", raw)
        self.assertNotIn(b"CREATE TABLE", raw)
        self.assertNotIn("Grüße".encode("utf-8"), raw)
        # Not a SQLite file either: the transport carries ciphertext, not a database.
        self.assertFalse(raw.startswith(b"SQLite format 3"))

    def test_tampered_ciphertext_is_rejected_and_nothing_is_imported(self) -> None:
        snapshot = self.a.publish()
        payload = bytearray(snapshot.path.read_bytes())
        payload[len(payload) // 2] ^= 0xFF  # flip one bit in the middle
        snapshot.path.write_bytes(bytes(payload))
        # Keep the manifest honest about size and hash, so only the cipher can object.
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
        import hashlib

        manifest["sha256"] = hashlib.sha256(snapshot.path.read_bytes()).hexdigest()
        manifest["size"] = snapshot.path.stat().st_size
        snapshot.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(SyncError) as caught:
            self.b.import_replica()
        self.assertIn("decryption failed", str(caught.exception))
        self.assertEqual([], list((self.root / "replicas").rglob("*.sqlite")))

    def test_truncated_snapshot_is_named_as_a_transfer_problem(self) -> None:
        snapshot = self.a.publish()
        snapshot.path.write_bytes(snapshot.path.read_bytes()[:-64])
        with self.assertRaises(SyncError) as caught:
            self.b.import_replica()
        self.assertIn("size mismatch", str(caught.exception))

    def test_foreign_key_cannot_import(self) -> None:
        self.a.publish()
        other_key = self.root / "keys" / "other.key"
        other_key.write_bytes(generate_key())
        config = self._config(self.b_db, "node-b")
        config.key_file = other_key
        with self.assertRaises(SyncError) as caught:
            ReplicaTransit(config).import_replica()
        self.assertIn("different key", str(caught.exception))

    def test_credential_scan_blocks_replica_publication(self) -> None:
        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute(
                "UPDATE items SET value = ? WHERE id = 'row-0'",
                ("deploy note: use " + "ghp_" + "E" * 24,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(SyncError) as caught:
            self.a.publish()
        message = str(caught.exception)
        self.assertIn("items.value", message)
        self.assertNotIn("E" * 24, message)
        self.assertEqual([], list(self.transit.rglob(f"*{'.sqlite-replica'}")))

    def test_excluded_tables_never_reach_the_replica(self) -> None:
        self.a.publish()
        report = self.b.import_replica()[0]
        connection = sqlite3.connect(f"file:{Path(report.replica_path).as_posix()}?mode=ro", uri=True)
        try:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM secrets").fetchone()[0])
        finally:
            connection.close()

    # -- key handling ----------------------------------------------------

    def test_key_inside_transit_is_refused(self) -> None:
        config = self._config(self.a_db, "node-a")
        config.key_file = self.transit / "leaked.key"
        with self.assertRaisesRegex(SyncError, "must not live inside the transit"):
            ReplicaTransit(config)

    def test_key_in_a_synced_folder_is_refused_but_can_be_waived(self) -> None:
        synced = self.root / "OneDrive" / "keys"
        synced.mkdir(parents=True)
        key = synced / "replica.key"
        key.write_bytes(generate_key())

        config = self._config(self.a_db, "node-a")
        config.key_file = key
        with self.assertRaisesRegex(SyncError, "synchronised folder"):
            ReplicaTransit(config)

        config.allow_key_in_synced_folder = True
        self.assertIsInstance(ReplicaTransit(config), ReplicaTransit)

    def test_missing_key_reports_how_to_create_one(self) -> None:
        config = self._config(self.a_db, "node-a")
        config.key_file = None
        with self.assertRaises(SyncError) as caught:
            ReplicaTransit(config).publish()
        self.assertIn("keygen", str(caught.exception))

    def test_replica_root_inside_transit_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be stored inside the transit"):
            SyncConfig(
                database=self.a_db,
                transit=self.transit,
                state=self.root / "s.json",
                node_id="node-a",
                namespace="memory",
                key_file=self.key_file,
                replica_root=self.transit / "replicas",
            )

    # -- manifest --------------------------------------------------------

    def test_manifest_describes_the_payload_without_leaking_local_paths(self) -> None:
        snapshot = self.a.publish()
        manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("replica", manifest["mode"])
        self.assertEqual("fernet", manifest["encryption"])
        self.assertEqual("gzip", manifest["compression"])
        self.assertEqual("node-a", manifest["node_id"])
        self.assertEqual("a.db", manifest["source_database"])
        self.assertEqual(["secrets"], manifest["redacted_tables"])
        self.assertIn("sha256", manifest)
        self.assertIn("payload_sha256", manifest)
        # A manifest travels through the transport: no absolute local path may ride along.
        serialized = json.dumps(manifest)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(str(self.a_db), serialized)

    def test_config_roundtrip_preserves_replica_settings(self) -> None:
        config = self._config(self.a_db, "node-a")
        restored = SyncConfig.from_file(config.write(self.root / "node.json"))
        self.assertEqual(config.key_file, restored.key_file)
        self.assertEqual(config.replica_root, restored.replica_root)


if __name__ == "__main__":
    unittest.main()
