"""JSON-friendly command-line interface for sqlite-transit-sync."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

from .core import SyncConfig, SyncError, TransitSync
from .replica import ReplicaTransit, generate_key


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sqlite-transit-sync",
        description="Synchronize independent local SQLite databases through verified snapshots.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Write a node configuration")
    init.add_argument("--config", required=True)
    init.add_argument("--database", required=True)
    init.add_argument("--transit", required=True)
    init.add_argument("--state")
    init.add_argument("--node-id", default=socket.gethostname())
    init.add_argument("--namespace", default="default")
    init.add_argument("--key-file", help="Fernet key for publish-replica mode")
    init.add_argument("--replica-root", help="Where imported replicas are stored")

    keygen = sub.add_parser("keygen", help="Write a new Fernet key for publish-replica mode")
    keygen.add_argument("--key-file", required=True)
    keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key (this orphans every snapshot encrypted with the old one)",
    )

    for name in ("status", "push", "pull", "sync", "verify", "list", "publish", "replicas"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "pull":
            command.add_argument("--dry-run", action="store_true")

    importer = sub.add_parser("import-replica", help="Import foreign snapshots as read-only replicas")
    importer.add_argument("--config", required=True)
    importer.add_argument("--node", help="Import only from this source node")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            config_path = Path(args.config).expanduser().resolve()
            state = args.state or str(config_path.with_name(f".{config_path.stem}-state.json"))
            config = SyncConfig(
                database=Path(args.database),
                transit=Path(args.transit),
                state=Path(state),
                node_id=args.node_id,
                namespace=args.namespace,
                key_file=Path(args.key_file) if args.key_file else None,
                replica_root=Path(args.replica_root) if args.replica_root else None,
            )
            written = config.write(config_path)
            _print({"ok": True, "config": str(written)})
            return 0

        if args.command == "keygen":
            key_path = Path(args.key_file).expanduser().resolve()
            if key_path.exists() and not args.force:
                raise SyncError(
                    f"Key file already exists: {key_path}. Replacing it makes every "
                    "snapshot encrypted with the old key unreadable; pass --force if "
                    "that is intended."
                )
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(generate_key())
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
            _print(
                {
                    "ok": True,
                    "key_file": str(key_path),
                    "note": "Distribute this key out of band. Never place it in the transit.",
                }
            )
            return 0

        config = SyncConfig.from_file(args.config)
        if args.command in ("publish", "replicas", "import-replica"):
            replica = ReplicaTransit(config)
            if args.command == "publish":
                result = replica.publish().as_dict()
            elif args.command == "replicas":
                result = [
                    {key: value for key, value in item.items() if not key.startswith("_")}
                    for item in replica.available()
                ]
            else:
                result = [item.as_dict() for item in replica.import_replica(node_id=args.node)]
            _print({"ok": True, "result": result})
            return 0

        sync = TransitSync(config)
        if args.command == "status":
            result = sync.status()
        elif args.command == "push":
            result = sync.push().as_dict()
        elif args.command == "pull":
            result = [report.as_dict() for report in sync.pull(dry_run=args.dry_run)]
        elif args.command == "sync":
            result = sync.sync()
        elif args.command == "verify":
            result = sync.verify()
        elif args.command == "list":
            result = [snapshot.as_dict() for snapshot in sync.snapshots(verify=False)]
        else:
            raise AssertionError(args.command)
        _print({"ok": True, "result": result})
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, SyncError) as error:
        _print({"ok": False, "error": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())

