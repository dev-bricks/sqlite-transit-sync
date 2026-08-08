from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
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
from sqlite_transit_sync.cli import build_parser, main as cli_main


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


def add_fixture_snapshot(sync: TransitSync, node_id: str, created_at: str) -> tuple[Path, Path]:
    """Publish a deterministic, anonymized manifest pair for retention tests."""
    name = f"{sync.config.namespace}__{node_id}__{created_at}.sqlite-snapshot"
    snapshot_path = sync.config.transit / name
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(sync.config.database.read_bytes())
    manifest_path = snapshot_path.with_suffix(snapshot_path.suffix + ".json")
    manifest_path.write_text(
        json.dumps(
            {
                "protocol": 1,
                "namespace": sync.config.namespace,
                "node_id": node_id,
                "created_at": created_at,
                "snapshot": snapshot_path.name,
                "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
                "size": snapshot_path.stat().st_size,
                "redacted_tables": ["secrets"],
            }
        ),
        encoding="utf-8",
    )
    return snapshot_path, manifest_path


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

    def test_vendor_prefixes_do_not_match_inside_ordinary_words(self) -> None:
        # A credential prefix starts a token; it never continues a word. Without a
        # left anchor, "sk-" matches inside "task-scheduler" and "ask-2026" — and
        # since the scan is fail-closed, such a hit blocks publication of an
        # otherwise clean database. Found on a real knowledge base: 33 hits, all words.
        self._set_item_value(
            self.a_db,
            "run antigravity-task-scheduler, see /analysen/ask-2026-auswertung "
            "and the risk-and-reward-analysis in Task-Management-Uebersicht",
        )
        snapshot = self.a.push()
        self.assertTrue(snapshot.path.is_file())

    def test_vendor_prefixes_still_match_a_real_key_in_context(self) -> None:
        # The counterpart to the test above: the anchor must not blind the scanner
        # to a key that sits after quotes, equals signs or whitespace.
        for prefix, filler in (("sk-", "A" * 32), ("sk-ant-api03-", "B" * 40), ("ghp_", "C" * 36)):
            with self.subTest(prefix=prefix):
                self._set_item_value(self.a_db, 'config: {"api_key": "' + prefix + filler + '"}')
                with self.assertRaises(SyncError):
                    self.a.push()
                self.assertEqual([], transit_files(self.transit))

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

    def test_cleanup_is_dry_run_and_local_node_only_by_default(self) -> None:
        own = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")
        foreign = add_fixture_snapshot(self.a, "node-b", "20200101T000000000000Z")

        report = self.a.cleanup(keep_days=7, keep_per_node=0)

        self.assertTrue(report["dry_run"])
        self.assertEqual("local-node", report["scope"])
        self.assertEqual(2, report["examined"])
        self.assertEqual(1, report["managed"])
        self.assertEqual(1, report["skipped_foreign"])
        self.assertEqual([own[0].name], [item["snapshot"] for item in report["eligible"]])
        self.assertEqual([], report["deleted"])
        self.assertTrue(all(path.exists() for path in (*own, *foreign)))

    def test_cleanup_apply_removes_snapshot_and_manifest_pair(self) -> None:
        old = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")

        report = self.a.cleanup(keep_days=7, keep_per_node=0, dry_run=False)

        self.assertEqual([old[0].name], [item["snapshot"] for item in report["deleted"]])
        self.assertTrue(all(not path.exists() for path in old))

    def test_cleanup_requires_explicit_foreign_node_scope(self) -> None:
        foreign = add_fixture_snapshot(self.a, "node-b", "20200101T000000000000Z")

        report = self.a.cleanup(
            keep_days=7,
            keep_per_node=0,
            include_foreign=True,
            dry_run=False,
        )

        self.assertEqual("all-nodes", report["scope"])
        self.assertEqual([foreign[0].name], [item["snapshot"] for item in report["deleted"]])
        self.assertTrue(all(not path.exists() for path in foreign))

    def test_cleanup_verifies_only_the_authorized_node_scope(self) -> None:
        own = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")
        foreign = add_fixture_snapshot(self.a, "node-b", "20200101T000000000000Z")
        foreign[0].write_bytes(foreign[0].read_bytes() + b"tampered")

        report = self.a.cleanup(keep_days=7, keep_per_node=0, dry_run=False)

        self.assertEqual([own[0].name], [item["snapshot"] for item in report["deleted"]])
        self.assertTrue(foreign[0].exists())
        with self.assertRaises(SyncError):
            self.a.cleanup(
                keep_days=7,
                keep_per_node=0,
                include_foreign=True,
            )

    def test_cleanup_rejects_manifest_alias_to_another_node_snapshot(self) -> None:
        foreign = add_fixture_snapshot(self.a, "node-b", "20200101T000000000000Z")
        alias_manifest = self.transit / (
            "demo__node-a__20200101T000000000000Z.sqlite-snapshot.json"
        )
        payload = json.loads(foreign[1].read_text(encoding="utf-8"))
        payload["node_id"] = "node-a"
        alias_manifest.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(SyncError):
            self.a.cleanup(keep_days=7, keep_per_node=0, dry_run=False)

        self.assertTrue(all(path.exists() for path in foreign))
        self.assertTrue(alias_manifest.exists())

    def test_cleanup_cli_reports_malformed_manifests_as_json_error(self) -> None:
        old = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")
        valid_payload = json.loads(old[1].read_text(encoding="utf-8"))
        wrong_created_at = dict(valid_payload, created_at=123)
        config_path = self.a.config.write(self.root / "node-a.json")

        for malformed, expected_error in (
            (wrong_created_at, "Invalid manifest value types"),
            (dict(valid_payload, protocol=True), "Unsupported protocol"),
            (dict(valid_payload, protocol=1.0), "Unsupported protocol"),
            (7, "Invalid manifest object"),
            (None, "Invalid manifest object"),
            (True, "Invalid manifest object"),
        ):
            with self.subTest(malformed=malformed):
                old[1].write_text(json.dumps(malformed), encoding="utf-8")
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = cli_main(["cleanup", "--config", str(config_path)])
                response = json.loads(output.getvalue())
                self.assertEqual(1, exit_code)
                self.assertFalse(response["ok"])
                self.assertIn(expected_error, response["error"])

    def test_cleanup_refuses_unexpected_snapshot_sidecars_without_deleting_them(self) -> None:
        old = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")
        unrelated = old[0].with_name(f"{old[0].name}-operator-notes")
        unrelated.write_text("unrelated", encoding="utf-8")

        with self.assertRaises(SyncError):
            self.a.cleanup(keep_days=7, keep_per_node=0, dry_run=False)

        self.assertTrue(all(path.exists() for path in old))
        self.assertTrue(unrelated.exists())

    def test_cleanup_restores_manifest_when_snapshot_delete_fails(self) -> None:
        old = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")
        real_unlink = Path.unlink

        def fail_snapshot_unlink(path: Path, *args, **kwargs):
            if path == old[0]:
                raise PermissionError("synthetic delete failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_snapshot_unlink):
            with self.assertRaises(SyncError):
                self.a.cleanup(keep_days=7, keep_per_node=0, dry_run=False)

        self.assertTrue(all(path.exists() for path in old))
        self.assertFalse(
            old[1].with_name(f".{old[1].name}.cleanup").exists()
        )

    def test_cleanup_keeps_latest_per_node_even_when_old(self) -> None:
        oldest = add_fixture_snapshot(self.a, "node-a", "20200101T000000000000Z")
        newest = add_fixture_snapshot(self.a, "node-a", "20200102T000000000000Z")

        report = self.a.cleanup(keep_days=7, keep_per_node=1, dry_run=False)

        self.assertEqual([oldest[0].name], [item["snapshot"] for item in report["deleted"]])
        self.assertTrue(all(path.exists() for path in newest))

    def test_cleanup_cli_is_non_destructive_without_apply(self) -> None:
        args = build_parser().parse_args(["cleanup", "--config", "node.json"])

        self.assertEqual("cleanup", args.command)
        self.assertFalse(args.apply)
        self.assertFalse(args.all_nodes)
        self.assertEqual(7, args.keep_days)
        self.assertEqual(10, args.keep_per_node)

    def test_cleanup_rejects_negative_retention_values(self) -> None:
        with self.assertRaises(ValueError):
            self.a.cleanup(keep_days=-1)
        with self.assertRaises(ValueError):
            self.a.cleanup(keep_per_node=-1)


if __name__ == "__main__":
    unittest.main()
