import fcntl
import os

import pytest

from duckdb_safe_snapshot import Config, SnapshotError, create_snapshot
from test_snapshot import make_config


def test_inherited_lock_descriptor_must_match_configured_lock(tmp_path):
    cfg = make_config(tmp_path)
    other = tmp_path / "other.lock"
    other.write_bytes(b"")
    with other.open("rb") as handle:
        inherited = Config(**{**cfg.__dict__, "inherited_lock_fd": handle.fileno()})
        with pytest.raises(SnapshotError, match="does not match"):
            create_snapshot(inherited)


def test_inherited_lock_descriptor_reuses_held_lock(tmp_path):
    cfg = make_config(tmp_path)
    with cfg.lock_path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        inherited = Config(**{**cfg.__dict__, "inherited_lock_fd": handle.fileno()})
        assert create_snapshot(inherited)["status"] == "created"
    with pytest.raises(SnapshotError, match="invalid or unavailable"):
        create_snapshot(Config(**{**cfg.__dict__, "inherited_lock_fd": 99999}))


@pytest.mark.parametrize("descriptor", [-1, True, False])
def test_inherited_lock_descriptor_rejects_invalid_types_and_values(tmp_path, descriptor):
    cfg = make_config(tmp_path)
    with pytest.raises(ValueError, match="non-negative descriptor"):
        Config(**{**cfg.__dict__, "inherited_lock_fd": descriptor})
