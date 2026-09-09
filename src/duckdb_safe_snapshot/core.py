"""Create and verify lock-coordinated DuckDB snapshot sets.

The caller supplies every filesystem and ownership policy through ``Config``.
All DuckDB writers must take the same advisory lock before changing the source.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn, Mapping


SNAPSHOT_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_NAME = "manifest.json"
MANIFEST_CHECKSUM_NAME = "manifest.sha256"
COPY_CHUNK_SIZE = 4 * 1024 * 1024
MAX_KEEP = 30


class SnapshotError(RuntimeError):
    """A snapshot operation failed closed before returning a trusted result."""


def fail(message: str) -> NoReturn:
    raise SnapshotError(message)


@dataclass(frozen=True)
class Config:
    """Filesystem and ownership policy for one snapshot installation.

    ``source_owner_uid`` owns the live database and WAL. ``lock_owner_uid`` owns
    the shared writer lock. ``snapshot_owner_uid`` owns snapshot/state roots and
    every completed artifact. Directory modes are intentionally fixed at 0700
    and completed file modes at 0600.
    """

    database_path: Path
    wal_path: Path
    lock_path: Path
    backup_root: Path
    state_root: Path
    source_owner_uid: int
    lock_owner_uid: int
    snapshot_owner_uid: int
    database_artifact_name: str
    wal_artifact_name: str
    metadata: Mapping[str, Any] | None = None
    release: str | Callable[[], str | None] | None = None

    def __post_init__(self) -> None:
        paths = {
            "database_path": Path(self.database_path),
            "wal_path": Path(self.wal_path),
            "lock_path": Path(self.lock_path),
            "backup_root": Path(self.backup_root),
            "state_root": Path(self.state_root),
        }
        if any(not value.is_absolute() for value in paths.values()):
            raise ValueError("snapshot paths must be absolute")
        lexical = {name: Path(os.path.abspath(value)) for name, value in paths.items()}
        resolved = {name: value.resolve(strict=False) for name, value in lexical.items()}
        if len(set(resolved.values())) != len(resolved):
            raise ValueError("snapshot paths must not alias each other")
        for name, value in lexical.items():
            object.__setattr__(self, name, value)
        roots = (resolved["backup_root"], resolved["state_root"])
        for name in ("database_path", "wal_path", "lock_path"):
            path = resolved[name]
            if any(_is_within(path, root) for root in roots):
                raise ValueError(f"{name} must not be inside a private output root")
        if _is_within(resolved["backup_root"], resolved["state_root"]) or _is_within(
            resolved["state_root"], resolved["backup_root"]
        ):
            raise ValueError("backup_root and state_root must not contain each other")
        for uid in (self.source_owner_uid, self.lock_owner_uid, self.snapshot_owner_uid):
            if not isinstance(uid, int) or uid < 0:
                raise ValueError("owner UIDs must be non-negative integers")
        for name in (self.database_artifact_name, self.wal_artifact_name):
            if not name or Path(name).name != name or name in {MANIFEST_NAME, MANIFEST_CHECKSUM_NAME}:
                raise ValueError("artifact names must be simple, distinct filenames")
        if self.database_artifact_name == self.wal_artifact_name:
            raise ValueError("database and WAL artifact names must differ")
        if self.metadata is not None:
            try:
                json.dumps(self.metadata, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("metadata must be JSON serializable") from exc
        if self.release is not None and not isinstance(self.release, str) and not callable(self.release):
            raise ValueError("release must be a string, callback, or None")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_private_directory(path: Path, cfg: Config, *, create: bool) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        fail(f"required private directory is missing: {path}")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != cfg.snapshot_owner_uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"unsafe private snapshot directory: {path}")


def validate_source_file(path: Path, cfg: Config, *, required: bool) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            fail(f"snapshot source is missing: {path}")
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_uid != cfg.source_owner_uid
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        fail(f"unsafe snapshot source: {path}")
    return metadata


def validate_lock(path: Path, cfg: Config) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        fail(f"shared writer lock is missing: {path}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or metadata.st_uid != cfg.lock_owner_uid
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        fail(f"unsafe shared writer lock: {path}")


@contextmanager
def writer_lock(cfg: Config, timeout_seconds: float) -> Iterator[None]:
    """Take the same advisory lock every participating writer uses."""
    validate_lock(cfg.lock_path, cfg)
    descriptor = os.open(cfg.lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fail("timed out waiting for the shared writer lock")
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def copy_and_hash(source_path: Path, destination_path: Path) -> dict[str, object]:
    source_fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            fail(f"unsafe opened snapshot source: {source_path}")
        destination_fd = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as source:
                with os.fdopen(destination_fd, "wb", closefd=False) as destination:
                    while chunk := source.read(COPY_CHUNK_SIZE):
                        destination.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
            os.fchmod(destination_fd, 0o600)
        finally:
            os.close(destination_fd)
        after = os.fstat(source_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            fail(f"snapshot source changed during copy: {source_path}")
    finally:
        os.close(source_fd)
    if size != before.st_size:
        fail(f"short snapshot copy for {source_path}: {size} != {before.st_size}")
    return {"name": destination_path.name, "sha256": digest.hexdigest(), "size": size}


def canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, separators=(",", ": ")) + "\n").encode("utf-8")


def write_bytes(path: Path, encoded: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def source_state(path: Path, cfg: Config, *, required: bool) -> tuple[int, int, int, int] | None:
    metadata = validate_source_file(path, cfg, required=required)
    if metadata is None:
        return None
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def snapshot_id_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def recognized_snapshots(cfg: Config) -> list[Path]:
    snapshots: list[Path] = []
    for candidate in cfg.backup_root.iterdir():
        if not SNAPSHOT_RE.fullmatch(candidate.name):
            continue
        metadata = candidate.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode) or candidate.is_symlink()
            or metadata.st_uid != cfg.snapshot_owner_uid or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            fail(f"unsafe recognized snapshot: {candidate}")
        snapshots.append(candidate)
    return sorted(snapshots, key=lambda value: value.name, reverse=True)


def prune_snapshots(cfg: Config, keep: int, *, preserve: str | None = None) -> list[str]:
    if keep < 1 or keep > MAX_KEEP:
        fail(f"keep must be between 1 and {MAX_KEEP}")
    removed: list[str] = []
    snapshots = recognized_snapshots(cfg)
    if preserve is not None:
        preserved = [snapshot for snapshot in snapshots if snapshot.name == preserve]
        if len(preserved) != 1:
            fail(f"completed snapshot is not recognized: {preserve}")
        snapshots = preserved + [snapshot for snapshot in snapshots if snapshot.name != preserve]
    for snapshot in snapshots[keep:]:
        verify_snapshot(cfg, snapshot.name)
        shutil.rmtree(snapshot)
        removed.append(snapshot.name)
    if removed:
        fsync_directory(cfg.backup_root)
    return removed


def create_snapshot(cfg: Config, *, keep: int = 4, timeout_seconds: float = 1800.0) -> dict[str, object]:
    """Create, verify, record, and retain an atomic snapshot set."""
    if keep < 1 or keep > MAX_KEEP:
        fail(f"keep must be between 1 and {MAX_KEEP}")
    if not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds < 0 or timeout_seconds > 3600:
        fail("lock timeout must be between 0 and 3600 seconds")
    validate_private_directory(cfg.backup_root, cfg, create=True)
    validate_private_directory(cfg.state_root, cfg, create=True)
    with writer_lock(cfg, timeout_seconds):
        snapshot_id = snapshot_id_now()
        final = cfg.backup_root / snapshot_id
        temporary = cfg.backup_root / f".tmp-{snapshot_id}-{os.getpid()}"
        if final.exists() or final.is_symlink() or temporary.exists() or temporary.is_symlink():
            fail(f"snapshot path already exists: {snapshot_id}")
        database_before = source_state(cfg.database_path, cfg, required=True)
        wal_before = source_state(cfg.wal_path, cfg, required=False)
        temporary.mkdir(mode=0o700)
        try:
            artifacts = [copy_and_hash(cfg.database_path, temporary / cfg.database_artifact_name)]
            if wal_before is not None:
                artifacts.append(copy_and_hash(cfg.wal_path, temporary / cfg.wal_artifact_name))
            if source_state(cfg.database_path, cfg, required=True) != database_before:
                fail("DuckDB changed while the shared writer lock was held")
            if source_state(cfg.wal_path, cfg, required=False) != wal_before:
                fail("DuckDB WAL changed while the shared writer lock was held")
            manifest: dict[str, object] = {
                "schema": 1, "snapshot_id": snapshot_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": str(cfg.database_path), "artifacts": artifacts,
            }
            if cfg.metadata is not None:
                manifest["metadata"] = dict(cfg.metadata)
            release = cfg.release() if callable(cfg.release) else cfg.release
            if release is not None:
                if not isinstance(release, str):
                    fail("release callback must return a string or None")
                manifest["release"] = release
            encoded_manifest = canonical_json(manifest)
            manifest_digest = hashlib.sha256(encoded_manifest).hexdigest()
            write_bytes(temporary / MANIFEST_NAME, encoded_manifest)
            write_bytes(temporary / MANIFEST_CHECKSUM_NAME, f"{manifest_digest}  {MANIFEST_NAME}\n".encode("ascii"))
            fsync_directory(temporary)
            os.replace(temporary, final)
            fsync_directory(cfg.backup_root)
            return finalize_snapshot(cfg, snapshot_id, keep)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def finalize_snapshot(cfg: Config, snapshot_id: str, keep: int) -> dict[str, object]:
    """Verify and publish state while the participating-writer lock is still held."""
    verified = verify_snapshot(cfg, snapshot_id)
    state = {"schema": 1, "snapshot_id": snapshot_id, "manifest_sha256": verified["manifest_sha256"], "verified_at": datetime.now(timezone.utc).isoformat()}
    state_path = cfg.state_root / "latest.json"
    state_temporary = cfg.state_root / f".latest.json.tmp-{os.getpid()}"
    if state_temporary.exists() or state_temporary.is_symlink():
        fail(f"unexpected snapshot state path: {state_temporary}")
    write_bytes(state_temporary, canonical_json(state))
    os.replace(state_temporary, state_path)
    fsync_directory(cfg.state_root)
    removed = prune_snapshots(cfg, keep, preserve=snapshot_id)
    return {"status": "created", "snapshot_id": snapshot_id, "manifest_sha256": verified["manifest_sha256"], "artifacts": verified["artifacts"], "pruned": removed}


def parse_manifest(snapshot: Path, cfg: Config) -> tuple[dict[str, object], bytes, str]:
    manifest_path = snapshot / MANIFEST_NAME
    checksum_path = snapshot / MANIFEST_CHECKSUM_NAME
    for path in (manifest_path, checksum_path):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            fail(f"missing snapshot metadata file: {path}")
        if (not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1
                or metadata.st_uid != cfg.snapshot_owner_uid or stat.S_IMODE(metadata.st_mode) != 0o600):
            fail(f"unsafe snapshot metadata file: {path}")
    encoded = manifest_path.read_bytes()
    actual_manifest_digest = hashlib.sha256(encoded).hexdigest()
    if checksum_path.read_text(encoding="ascii").strip() != f"{actual_manifest_digest}  {MANIFEST_NAME}":
        fail(f"snapshot manifest checksum mismatch: {snapshot.name}")
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as exc:
        fail(f"invalid snapshot manifest {snapshot.name}: {exc}")
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        fail(f"unsupported snapshot manifest: {snapshot.name}")
    return payload, encoded, actual_manifest_digest


def verify_snapshot(cfg: Config, snapshot_id: str) -> dict[str, object]:
    if not SNAPSHOT_RE.fullmatch(snapshot_id):
        fail(f"invalid snapshot id: {snapshot_id!r}")
    snapshot = cfg.backup_root / snapshot_id
    try:
        metadata = snapshot.lstat()
    except FileNotFoundError:
        fail(f"snapshot is missing: {snapshot}")
    if (not stat.S_ISDIR(metadata.st_mode) or snapshot.is_symlink() or metadata.st_uid != cfg.snapshot_owner_uid
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        fail(f"unsafe snapshot: {snapshot}")
    payload, _, manifest_digest = parse_manifest(snapshot, cfg)
    if payload.get("snapshot_id") != snapshot_id:
        fail(f"snapshot id mismatch: {snapshot_id}")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 2:
        fail(f"invalid snapshot artifact inventory: {snapshot_id}")
    allowed_names = {cfg.database_artifact_name, cfg.wal_artifact_name}
    verified: list[dict[str, object]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            fail(f"invalid snapshot artifact entry: {snapshot_id}")
        name, expected_digest, expected_size = artifact.get("name"), artifact.get("sha256"), artifact.get("size")
        if (not isinstance(name, str) or name not in allowed_names or name in seen
                or not isinstance(expected_digest, str) or not SHA256_RE.fullmatch(expected_digest)
                or not isinstance(expected_size, int) or expected_size < 0):
            fail(f"invalid snapshot artifact metadata: {snapshot_id}")
        seen.add(name)
        path = snapshot / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            fail(f"unsafe or mismatched snapshot artifact: {path}")
        if (not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1
                or metadata.st_uid != cfg.snapshot_owner_uid or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != expected_size):
            fail(f"unsafe or mismatched snapshot artifact: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(COPY_CHUNK_SIZE), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_digest:
            fail(f"snapshot artifact checksum mismatch: {path}")
        verified.append({"name": name, "sha256": expected_digest, "size": expected_size})
    if cfg.database_artifact_name not in seen:
        fail(f"snapshot is missing {cfg.database_artifact_name}: {snapshot_id}")
    expected_entries = {cfg.database_artifact_name, MANIFEST_NAME, MANIFEST_CHECKSUM_NAME}
    if cfg.wal_artifact_name in seen:
        expected_entries.add(cfg.wal_artifact_name)
    actual_entries = {entry.name for entry in snapshot.iterdir()}
    if actual_entries != expected_entries:
        fail(f"unexpected snapshot entries: {sorted(actual_entries - expected_entries)}")
    return {"status": "verified", "snapshot_id": snapshot_id, "manifest_sha256": manifest_digest, "release": payload.get("release"), "metadata": payload.get("metadata"), "artifacts": verified}


def latest_snapshot_state(cfg: Config) -> tuple[str, str]:
    state_path = cfg.state_root / "latest.json"
    try:
        metadata = state_path.lstat()
    except FileNotFoundError:
        fail(f"snapshot state file is missing: {state_path}")
    if (not stat.S_ISREG(metadata.st_mode) or state_path.is_symlink() or metadata.st_nlink != 1
            or metadata.st_uid != cfg.snapshot_owner_uid or stat.S_IMODE(metadata.st_mode) != 0o600):
        fail(f"unsafe snapshot state file: {state_path}")
    try:
        payload = json.loads(state_path.read_text())
    except json.JSONDecodeError as exc:
        fail(f"invalid snapshot state: {exc}")
    snapshot_id = payload.get("snapshot_id") if isinstance(payload, dict) else None
    manifest_digest = payload.get("manifest_sha256") if isinstance(payload, dict) else None
    if not isinstance(snapshot_id, str) or not SNAPSHOT_RE.fullmatch(snapshot_id):
        fail("snapshot state has no valid latest snapshot")
    if payload.get("schema") != 1 or not isinstance(manifest_digest, str) or not SHA256_RE.fullmatch(manifest_digest):
        fail("snapshot state has no valid latest manifest checksum")
    return snapshot_id, manifest_digest


def verify_latest(cfg: Config) -> dict[str, object]:
    snapshot_id, expected_manifest_digest = latest_snapshot_state(cfg)
    payload = verify_snapshot(cfg, snapshot_id)
    if payload["manifest_sha256"] != expected_manifest_digest:
        fail("latest snapshot state does not match the snapshot manifest")
    return payload
