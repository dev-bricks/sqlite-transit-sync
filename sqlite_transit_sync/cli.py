"""JSON-friendly command-line interface for sqlite-transit-sync."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

from .core import SyncConfig, SyncError, TransitSync
from .republica import RepublicaTransit, generate_key


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
    init.add_argument("--key-file", help="Fernet key for Republica mode")
    init.add_argument("--republica-root", help="Where imported Republica showcases are stored")

    keygen = sub.add_parser("keygen", help="Write a new Fernet key for Republica mode")
    keygen.add_argument("--key-file", required=True)
    keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key (this orphans every snapshot encrypted with the old one)",
    )

    for name in ("status", "push", "pull", "sync", "verify", "list"):
        command = sub.add_parser(name)
        command.add_argument("--config", required=True)
        if name == "pull":
            command.add_argument("--dry-run", action="store_true")

    # Republica ("showcase method"): one-way, encrypted, never merged.
    sub.add_parser(
        "republica-publish", help="Publish this node's database as an encrypted showcase"
    ).add_argument("--config", required=True)
    sub.add_parser(
        "republica-list", help="List showcases other nodes offer in the transit"
    ).add_argument("--config", required=True)
    importer = sub.add_parser(
        "republica-import", help="Import foreign showcases as separate read-only databases"
    )
    importer.add_argument("--config", required=True)
    importer.add_argument("--node", help="Import only from this source node")

    # Sealed envelope: a single encrypted file over the same channel and key.
    # It never touches a database - see ADR-009.
    send = sub.add_parser(
        "envelope-send", help="Send one file encrypted through the transit (never enters a database)"
    )
    send.add_argument("--config", required=True)
    send.add_argument("--file", required=True, help="File to seal")
    send.add_argument("--to", help="Recipient node id (informational; any key holder can open it)")
    send.add_argument("--label", help="Short purpose shown to the recipient, e.g. 'hetzner-api-token'")

    receive = sub.add_parser(
        "envelope-receive", help="Unseal envelopes into a local directory, as files"
    )
    receive.add_argument("--config", required=True)
    receive.add_argument(
        "--into",
        required=True,
        help="Target directory, e.g. your credentials folder. Never a database.",
    )
    receive.add_argument("--node", help="Only from this source node")
    receive.add_argument(
        "--keep",
        action="store_true",
        help="Keep the envelope in the transit after unsealing (default: remove it)",
    )
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
                republica_root=Path(args.republica_root) if args.republica_root else None,
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
        if args.command.startswith("republica-") or args.command.startswith("envelope-"):
            republica = RepublicaTransit(config)
            if args.command == "republica-publish":
                result = republica.publish().as_dict()
            elif args.command == "republica-list":
                result = [
                    {key: value for key, value in item.items() if not key.startswith("_")}
                    for item in republica.available()
                ]
            elif args.command == "republica-import":
                result = [item.as_dict() for item in republica.import_republica(node_id=args.node)]
            elif args.command == "envelope-send":
                result = republica.envelope_send(
                    Path(args.file), recipient=args.to, label=args.label
                ).as_dict()
            else:
                result = [
                    item.as_dict()
                    for item in republica.envelope_receive(
                        Path(args.into), node_id=args.node, keep=args.keep
                    )
                ]
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

