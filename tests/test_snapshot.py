from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from duckdb_safe_snapshot import Config, SnapshotError, create_snapshot, verify_latest, verify_snapshot


def make_config(tmp_path: Path) -> Config:
    root = tmp_path / "fixture"
    for name in ("database", "locks", "snapshots", "state"):
        path = root / name
        path.mkdir(parents=True)
        path.chmod(0o700)
    database = root / "database" / "demo.duckdb"
    database.write_bytes(b"placeholder")
    database.chmod(0o600)
    lock = root / "locks" / "writer.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    uid = os.geteuid()
    return Config(
        database_path=database,
        lock_path=lock,
        backup_root=root / "snapshots",
        state_root=root / "state",
        source_owner_uid=uid,
        lock_owner_uid=uid,
        snapshot_owner_uid=uid,
        database_artifact_name="database.duckdb",
        wal_artifact_name="demo.duckdb.wal",
        metadata={"application": "synthetic-demo"},
    )


def snapshot_dir(cfg: Config, payload: dict[str, object]) -> Path:
    return cfg.backup_root / str(payload["snapshot_id"])


def test_snapshot_copies_real_duckdb_and_wal_that_reopens(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.database_path.unlink()
    connection = duckdb.connect(str(cfg.database_path))
    try:
        connection.execute("CREATE TABLE probe (id INTEGER, label VARCHAR)")
        connection.execute("INSERT INTO probe VALUES (7, 'consistent')")
        created = create_snapshot(cfg, keep=4)
    finally:
        connection.close()

    created_dir = snapshot_dir(cfg, created)
    assert {item["name"] for item in created["artifacts"]} == {"database.duckdb", "demo.duckdb.wal"}
    assert json.loads((created_dir / "manifest.json").read_text())["metadata"] == {"application": "synthetic-demo"}
    recovered = tmp_path / "recovered.duckdb"
    shutil.copyfile(created_dir / "database.duckdb", recovered)
    shutil.copyfile(created_dir / "demo.duckdb.wal", recovered.with_name("recovered.duckdb.wal"))
    restored = duckdb.connect(str(recovered))
    try:
        assert restored.execute("SELECT * FROM probe").fetchall() == [(7, "consistent")]
    finally:
        restored.close()
    assert verify_latest(cfg)["snapshot_id"] == created["snapshot_id"]


def test_participating_writer_lock_serializes_snapshot(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    acquired = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with cfg.lock_path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired.set()
            release.wait(timeout=2)
            cfg.database_path.write_bytes(b"writer-complete")
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    worker = threading.Thread(target=writer)
    worker.start()
    assert acquired.wait(timeout=1)
    with pytest.raises(SnapshotError, match="timed out waiting"):
        create_snapshot(cfg, timeout_seconds=0)
    release.set()
    worker.join(timeout=2)
    created = create_snapshot(cfg, timeout_seconds=1)
    assert (snapshot_dir(cfg, created) / "database.duckdb").read_bytes() == b"writer-complete"


def test_interrupted_copy_leaves_no_complete_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    import duckdb_safe_snapshot.core as core

    def interrupted(source: Path, destination: Path) -> dict[str, object]:
        destination.write_bytes(b"partial")
        raise KeyboardInterrupt()

    monkeypatch.setattr(core, "copy_and_hash", interrupted)
    with pytest.raises(KeyboardInterrupt):
        create_snapshot(cfg)
    assert not [path for path in cfg.backup_root.iterdir() if path.name[:1] != "."]
    assert not (cfg.state_root / "latest.json").exists()


def test_verify_rejects_tampered_or_incomplete_sets_and_retention_keeps_unrecognized_files(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    first = create_snapshot(cfg, keep=4)
    # A non-snapshot file has no recognized-id shape and is never retention input.
    legacy = cfg.backup_root / "unrelated-backup.bin"
    legacy.write_bytes(b"keep")
    cfg.database_path.write_bytes(b"fresh")
    second = create_snapshot(cfg, keep=1)
    assert legacy.read_bytes() == b"keep"
    assert snapshot_dir(cfg, second).is_dir()

    directory = snapshot_dir(cfg, second)
    (directory / "database.duckdb").write_bytes(b"other")
    with pytest.raises(SnapshotError, match="checksum mismatch"):
        verify_snapshot(cfg, str(second["snapshot_id"]))

    (directory / "database.duckdb").unlink()
    with pytest.raises(SnapshotError, match="unsafe or mismatched"):
        verify_snapshot(cfg, str(second["snapshot_id"]))

    assert not snapshot_dir(cfg, first).exists()


def test_rejects_unsafe_source_link_and_snapshot_link(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.database_path.unlink()
    cfg.database_path.symlink_to(cfg.lock_path)
    with pytest.raises(SnapshotError, match="unsafe snapshot source"):
        create_snapshot(cfg)

    cfg.database_path.unlink()
    cfg.database_path.write_bytes(b"safe")
    created = create_snapshot(cfg)
    directory = snapshot_dir(cfg, created)
    target = directory / "database.duckdb"
    target.unlink()
    target.symlink_to(cfg.lock_path)
    with pytest.raises(SnapshotError, match="unsafe or mismatched"):
        verify_snapshot(cfg, str(created["snapshot_id"]))


def test_config_rejects_output_aliases_and_non_explicit_names(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    with pytest.raises(ValueError, match="must not alias"):
        Config(**{**cfg.__dict__, "state_root": cfg.backup_root})
    with pytest.raises(ValueError, match="inside a private output root"):
        Config(**{**cfg.__dict__, "database_path": cfg.backup_root / "database.duckdb"})
    with pytest.raises(ValueError, match="simple, distinct"):
        Config(**{**cfg.__dict__, "database_artifact_name": "nested/database.duckdb"})


def test_completed_set_is_private_and_atomic(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    created = create_snapshot(cfg)
    directory = snapshot_dir(cfg, created)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("database.duckdb", "manifest.json", "manifest.sha256"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert not list(cfg.backup_root.glob(".tmp-*"))
