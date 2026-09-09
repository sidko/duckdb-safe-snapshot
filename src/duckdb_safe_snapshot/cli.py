"""Command-line interface for :mod:`duckdb_safe_snapshot`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .core import Config, SnapshotError, create_snapshot, verify_latest, verify_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and verify lock-coordinated DuckDB snapshot sets.")
    parser.add_argument("--database", type=Path, required=True, help="absolute live DuckDB database path")
    parser.add_argument("--lock", type=Path, required=True, help="absolute shared participating-writer lock path")
    parser.add_argument("--backup-root", type=Path, required=True, help="absolute private snapshot directory")
    parser.add_argument("--state-root", type=Path, required=True, help="absolute private state directory")
    parser.add_argument("--source-owner-uid", type=int, required=True)
    parser.add_argument("--lock-owner-uid", type=int, required=True)
    parser.add_argument("--snapshot-owner-uid", type=int, required=True)
    parser.add_argument("--database-artifact", required=True, help="database filename inside a snapshot")
    parser.add_argument("--wal-artifact", required=True, help="WAL source and filename inside a snapshot")
    parser.add_argument("--metadata-json", help="optional JSON object recorded in each manifest")
    subcommands = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subcommands.add_parser("snapshot")
    snapshot_parser.add_argument("--keep", type=int, default=4)
    snapshot_parser.add_argument("--lock-timeout", type=float, default=1800.0)
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("snapshot_id", nargs="?", default="latest")
    return parser.parse_args()


def config_from_args(args: argparse.Namespace) -> Config:
    metadata = None
    if args.metadata_json is not None:
        try:
            metadata = json.loads(args.metadata_json)
        except json.JSONDecodeError as exc:
            raise SnapshotError(f"invalid metadata JSON: {exc}") from exc
        if not isinstance(metadata, dict):
            raise SnapshotError("metadata JSON must be an object")
    return Config(
        database_path=args.database,
        lock_path=args.lock,
        backup_root=args.backup_root,
        state_root=args.state_root,
        source_owner_uid=args.source_owner_uid,
        lock_owner_uid=args.lock_owner_uid,
        snapshot_owner_uid=args.snapshot_owner_uid,
        database_artifact_name=args.database_artifact,
        wal_artifact_name=args.wal_artifact,
        metadata=metadata,
    )


def main() -> None:
    try:
        args = parse_args()
        cfg = config_from_args(args)
        if args.command == "snapshot":
            payload = create_snapshot(cfg, keep=args.keep, timeout_seconds=args.lock_timeout)
        elif args.snapshot_id == "latest":
            payload = verify_latest(cfg)
        else:
            payload = verify_snapshot(cfg, args.snapshot_id)
    except (SnapshotError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
