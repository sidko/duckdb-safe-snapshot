"""Safe, lock-coordinated DuckDB file snapshot sets."""

from .core import Config, SnapshotError, create_snapshot, verify_latest, verify_snapshot, writer_lock

__all__ = ["Config", "SnapshotError", "create_snapshot", "verify_latest", "verify_snapshot", "writer_lock"]
__version__ = "0.1.0"
