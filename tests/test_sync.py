from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlite_transit_sync import (
    SyncConfig,
    SyncError,
    TransitSync,
    load_secret_patterns,
)


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
CREATE TABLE local_only (
    id TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def create_db(path: Path, item_value: str, timestamp: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO items VALUES ('shared', ?, ?)", (item_value, timestamp))
        connection.execute("INSERT INTO secrets VALUES ('token', ?, ?)", (item_value, timestamp))
        connection.execute("INSERT INTO local_only VALUES ('local', ?)", (item_value,))
        connection.commit()
    finally:
        connection.close()


def item(path: Path, table: str, key: str) -> tuple | None:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT * FROM [{table}] WHERE id = ?", (key,)).fetchone()
    finally:
        connection.close()


def transit_files(path: Path) -> list[str]:
    return sorted(item.name for item in path.iterdir()) if path.exists() else []


class TransitSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transit = self.root / "transit"
        self.a_db = self.root / "a.db"
        self.b_db = self.root / "b.db"
        create_db(self.a_db, "A1", "2026-01-01T00:00:00Z")
        create_db(self.b_db, "B0", "2025-01-01T00:00:00Z")
        self.a = TransitSync(
            SyncConfig(self.a_db, self.transit, self.root / "a-state.json", "node-a", "demo")
        )
        self.b = TransitSync(
            SyncConfig(self.b_db, self.transit, self.root / "b-state.json", "node-b", "demo")
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_roundtrip_is_row_level_idempotent_and_excludes_secrets(self) -> None:
        snapshot_a = self.a.push()
        self.assertTrue(snapshot_a.manifest_path.is_file())
        self.assertIsNone(item(snapshot_a.path, "secrets", "token"))
        reports = self.b.pull()
        self.assertEqual(1, reports[0].updated)
        self.assertEqual("A1", item(self.b_db, "items", "shared")[1])
        self.assertEqual("B0", item(self.b_db, "secrets", "token")[1])
        self.assertEqual([], self.b.pull())

        connection = sqlite3.connect(self.b_db)
        try:
            connection.execute(
                "UPDATE items SET value = ?, updated_at = ? WHERE id = 'shared'",
                ("B2", "2027-01-01T00:00:00Z"),
            )
            connection.execute(
                "INSERT INTO items VALUES ('new-on-b', 'new', '2027-01-01T00:00:00Z')"
            )
            connection.commit()
        finally:
            connection.close()
        self.b.push()
        reports = self.a.pull()
        self.assertEqual(1, reports[-1].updated)
        self.assertEqual(1, reports[-1].inserted)
        self.assertEqual("B2", item(self.a_db, "items", "shared")[1])
        self.assertEqual("new", item(self.a_db, "items", "new-on-b")[1])

    def test_snapshot_redaction_removes_secret_bytes_from_file(self) -> None:
        sensitive_value = "snapshot-redaction-fixture-2026-07-13"
        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute(
                "UPDATE secrets SET value = ?, updated_at = ? WHERE id = 'token'",
                (sensitive_value, "2026-02-01T00:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = self.a.push()

        self.assertIsNone(item(snapshot.path, "secrets", "token"))
        self.assertNotIn(sensitive_value.encode("utf-8"), snapshot.path.read_bytes())

    def test_own_snapshot_is_not_pending(self) -> None:
        self.a.push()
        self.assertEqual([], self.a.pending())
        self.assertEqual(1, len(self.b.pending()))

    def test_checksum_mismatch_blocks_pull_without_advancing_state(self) -> None:
        snapshot = self.a.push()
        snapshot.path.write_bytes(snapshot.path.read_bytes() + b"tampered")
        with self.assertRaises(SyncError):
            self.b.pull()
        self.assertFalse((self.root / "b-state.json").exists())

    def test_config_roundtrip_and_relative_paths(self) -> None:
        config_path = self.root / "config" / "node.json"
        config_path.parent.mkdir()
        config_path.write_text(
            json.dumps(
                {
                    "database": "../a.db",
                    "transit": "../transit",
                    "state": "state.json",
                    "node_id": "neutral node",
                    "namespace": "sample app",
                }
            ),
            encoding="utf-8",
        )
        config = SyncConfig.from_file(config_path)
        self.assertEqual("neutral-node", config.node_id)
        self.assertEqual("sample-app", config.namespace)
        self.assertEqual((self.root / "a.db").resolve(), config.database)
        self.assertEqual((config_path.parent / "state.json").resolve(), config.state)

    def test_config_from_bytes_uses_the_same_source_relative_semantics(self) -> None:
        config_path = self.root / "config" / "node.json"
        payload = json.dumps(
            {
                "database": "../a.db",
                "transit": "../transit",
                "state": "state.json",
                "node_id": "neutral node",
                "namespace": "sample app",
            }
        ).encode("utf-8")

        config = SyncConfig.from_bytes(payload, source_path=config_path)

        self.assertEqual((self.root / "a.db").resolve(), config.database)
        self.assertEqual((self.root / "transit").resolve(), config.transit)
        self.assertEqual((config_path.parent / "state.json").resolve(), config.state)
        self.assertEqual("neutral-node", config.node_id)
        self.assertEqual("sample-app", config.namespace)

    def test_wal_source_push_publishes_one_closed_snapshot_without_sidecars(self) -> None:
        connection = sqlite3.connect(self.a_db)
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            connection.execute(
                "UPDATE items SET value = 'from-wal', updated_at = '2029-01-01T00:00:00Z' "
                "WHERE id = 'shared'"
            )
            connection.commit()
            self.assertTrue(self.a_db.with_name(f"{self.a_db.name}-wal").is_file())

            snapshot = self.a.push()
        finally:
            connection.close()

        self.assertEqual("from-wal", item(snapshot.path, "items", "shared")[1])
        connection = sqlite3.connect(snapshot.path)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("delete", journal_mode)
        self.assertEqual(
            sorted((snapshot.path.name, snapshot.manifest_path.name)),
            transit_files(self.transit),
        )

    def test_wal_source_scanner_failure_removes_partial_snapshot_and_sidecars(self) -> None:
        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "UPDATE items SET value = ? WHERE id = 'shared'",
                ("deploy note: use " + "ghp_" + "D" * 24,),
            )
            connection.commit()
            with self.assertRaises(SyncError):
                self.a.push()
        finally:
            connection.close()

        self.assertEqual([], transit_files(self.transit))

    def test_wal_source_quick_check_failure_removes_partial_snapshot_and_sidecars(self) -> None:
        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "UPDATE items SET value = 'from-wal' WHERE id = 'shared'"
            )
            connection.commit()
            with (
                mock.patch.object(TransitSync, "_quick_check", side_effect=SyncError("forced")),
                self.assertRaises(SyncError),
            ):
                self.a.push()
        finally:
            connection.close()

        self.assertEqual([], transit_files(self.transit))

    def test_unrecognized_sqlite_sidecar_fails_closed_and_is_removed(self) -> None:
        def leave_sidecar(path: Path) -> None:
            path.with_name(f"{path.name}-future-sidecar").write_bytes(b"temporary")

        with (
            mock.patch.object(TransitSync, "_quick_check", side_effect=leave_sidecar),
            self.assertRaisesRegex(SyncError, "Unclosed SQLite snapshot"),
        ):
            self.a.push()

        self.assertEqual([], transit_files(self.transit))

    def test_wal_source_manifest_failure_removes_snapshot_and_sidecars(self) -> None:
        def fail_manifest(path: Path, _payload: dict) -> None:
            path.with_name(f".{path.name}.forced.tmp").write_text(
                "partial manifest",
                encoding="utf-8",
            )
            raise OSError("forced")

        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "UPDATE items SET value = 'from-wal' WHERE id = 'shared'"
            )
            connection.commit()
            with (
                mock.patch(
                    "sqlite_transit_sync.core._atomic_json_write",
                    side_effect=fail_manifest,
                ),
                self.assertRaises(OSError),
            ):
                self.a.push()
        finally:
            connection.close()

        self.assertEqual([], transit_files(self.transit))

    def test_backup_failure_removes_partial_snapshot_artifacts(self) -> None:
        invalid_database = self.root / "invalid.db"
        invalid_database.write_bytes(b"not a SQLite database")
        sync = TransitSync(
            SyncConfig(
                invalid_database,
                self.transit,
                self.root / "invalid-state.json",
                "node-invalid",
                "demo",
            )
        )

        with self.assertRaises(sqlite3.DatabaseError):
            sync.push()

        self.assertEqual([], transit_files(self.transit))

    def test_pull_requires_precreated_application_schema(self) -> None:
        self.a.push()
        missing = TransitSync(
            SyncConfig(
                self.root / "missing.db",
                self.transit,
                self.root / "missing-state.json",
                "node-c",
                "demo",
            )
        )
        with self.assertRaises(SyncError):
            missing.pull()

    def test_equal_timestamp_conflict_converges_deterministically(self) -> None:
        timestamp = "2026-02-01T00:00:00Z"
        for database, value in ((self.a_db, "alpha"), (self.b_db, "omega")):
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE items SET value = ?, updated_at = ? WHERE id = 'shared'",
                    (value, timestamp),
                )
                connection.commit()
            finally:
                connection.close()
        self.a.push()
        self.b.push()
        self.a.pull()
        self.b.pull()
        self.assertEqual(item(self.a_db, "items", "shared"), item(self.b_db, "items", "shared"))
        self.assertEqual("omega", item(self.a_db, "items", "shared")[1])

    def test_schema_drift_uses_only_shared_columns(self) -> None:
        connection = sqlite3.connect(self.a_db)
        try:
            connection.execute("ALTER TABLE items ADD COLUMN local_note TEXT")
            connection.execute(
                "UPDATE items SET value = 'newer', updated_at = '2028-01-01T00:00:00Z', "
                "local_note = 'only-on-a' WHERE id = 'shared'"
            )
            connection.commit()
        finally:
            connection.close()
        self.a.push()
        report = self.b.pull()[0]
        self.assertEqual(1, report.updated)
        self.assertEqual("newer", item(self.b_db, "items", "shared")[1])

    # -- credential scan -------------------------------------------------
    # Test fixtures below build token-shaped strings by concatenation on purpose:
    # a contiguous literal would be flagged by this repository's own secret gate
    # and by third-party scanners cloning the project.

    def _set_item_value(self, path: Path, value: str) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.execute("UPDATE items SET value = ? WHERE id = 'shared'", (value,))
            connection.commit()
        finally:
            connection.close()

    def test_secret_scan_blocks_publication_and_leaves_transit_clean(self) -> None:
        self._set_item_value(self.a_db, "deploy note: use " + "ghp_" + "A" * 24)
        with self.assertRaises(SyncError) as caught:
            self.a.push()
        message = str(caught.exception)
        self.assertIn("items.value", message)
        # The credential itself must never reach logs or exceptions.
        self.assertNotIn("A" * 24, message)
        # Nothing half-written may survive in the transit directory.
        leftovers = list(self.transit.glob("*")) if self.transit.exists() else []
        self.assertEqual([], leftovers)

    def test_secret_scan_ignores_hashes_and_identifiers(self) -> None:
        # Checksums, UUIDs and git SHAs are legitimate database content.
        self._set_item_value(
            self.a_db,
            "sha256=" + "a1b2c3d4" * 8 + " uuid=123e4567-e89b-12d3-a456-426614174000",
        )
        snapshot = self.a.push()
        self.assertTrue(snapshot.path.is_file())

    def test_secret_scan_is_optional(self) -> None:
        config = SyncConfig(
            self.a_db,
            self.transit,
            self.root / "a-state.json",
            "node-a",
            "demo",
            scan_snapshot_for_secrets=False,
        )
        self._set_item_value(self.a_db, "token " + "ghp_" + "B" * 24)
        snapshot = TransitSync(config).push()
        self.assertTrue(snapshot.path.is_file())

    def test_bundled_trigger_file_is_present_and_loadable(self) -> None:
        # Guards the packaging side: without package-data the JSON never reaches
        # an installed wheel and every push would fail.
        patterns = load_secret_patterns()
        self.assertTrue(patterns)
        self.assertTrue(all(p.name and p.regex for p in patterns))

    def test_custom_trigger_file_replaces_defaults(self) -> None:
        triggers = self.root / "my-triggers.json"
        triggers.write_text(
            json.dumps({"patterns": [{"name": "house", "regex": "ACME-[0-9]{4}", "prefilter": "ACME-"}]}),
            encoding="utf-8",
        )
        config = SyncConfig(
            self.a_db, self.transit, self.root / "a-state.json", "node-a", "demo",
            secret_patterns_file=triggers,
        )
        sync = TransitSync(config)
        # A GitHub-shaped token is no longer a trigger once defaults are replaced.
        self._set_item_value(self.a_db, "token " + "ghp_" + "C" * 24)
        self.assertTrue(sync.push().path.is_file())
        # The house pattern is.
        self._set_item_value(self.a_db, "internal ACME-1234")
        with self.assertRaises(SyncError) as caught:
            sync.push()
        self.assertIn("house", str(caught.exception))

    def test_pattern_without_prefilter_still_matches(self) -> None:
        # A pattern with no literal makes the SQL pre-filter unsound; the scanner
        # must fall back to reading the column instead of silently missing rows.
        triggers = self.root / "no-prefilter.json"
        triggers.write_text(
            json.dumps({"patterns": [{"name": "loose", "regex": "[0-9]{3}-secret-[0-9]{3}"}]}),
            encoding="utf-8",
        )
        config = SyncConfig(
            self.a_db, self.transit, self.root / "a-state.json", "node-a", "demo",
            secret_patterns_file=triggers,
        )
        self._set_item_value(self.a_db, "value 123-secret-456 inside")
        with self.assertRaises(SyncError) as caught:
            TransitSync(config).push()
        self.assertIn("loose", str(caught.exception))

    def test_broken_trigger_file_fails_loudly(self) -> None:
        triggers = self.root / "broken.json"
        triggers.write_text(json.dumps({"patterns": [{"name": "bad", "regex": "([unclosed"}]}), encoding="utf-8")
        config = SyncConfig(
            self.a_db, self.transit, self.root / "a-state.json", "node-a", "demo",
            secret_patterns_file=triggers,
        )
        with self.assertRaises(SyncError):
            TransitSync(config).push()

    def test_secret_scan_survives_config_roundtrip(self) -> None:
        config = SyncConfig(
            self.a_db,
            self.transit,
            self.root / "a-state.json",
            "node-a",
            "demo",
            scan_snapshot_for_secrets=False,
            secret_scan_skip_tables=("items",),
        )
        target = config.write(self.root / "config.json")
        restored = SyncConfig.from_file(target)
        self.assertFalse(restored.scan_snapshot_for_secrets)
        self.assertEqual(("items",), restored.secret_scan_skip_tables)


if __name__ == "__main__":
    unittest.main()
