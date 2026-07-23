"""JSON-friendly command-line interface for sqlite-transit-sync."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

from .core import SyncConfig, SyncError, TransitSync


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

    for name in ("status", "push", "pull", "sync", "verify", "list"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "pull":
            command.add_argument("--dry-run", action="store_true")
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
            )
            written = config.write(config_path)
            _print({"ok": True, "config": str(written)})
            return 0

        sync = TransitSync(SyncConfig.from_file(args.config))
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

