# Agent integration guide

`duckdb-safe-snapshot` creates and verifies lock-coordinated DuckDB database
and WAL snapshot sets on Linux. It requires an external `flock` path that every
participating writer acquires before modifying either source file.

Install a pinned release with `python -m pip install duckdb-safe-snapshot==0.1.0`.
Add it to the consumer's Python dependency file and lockfile, then configure an
explicit `Config` with absolute database, WAL, lock, backup, and state paths plus
source/lock/snapshot owner UIDs and database/WAL artifact names. Application
scheduling, service ordering, ACL setup, backup retention policy, and recovery
remain consumer-owned policy.

Before integration, inspect the consumer's writers and confirm each takes the
same lock. Use a disposable DuckDB database to run `create_snapshot(config)` and
`verify_latest(config)`, then copy the database and optional WAL artifacts to a
recovery filename and reopen it with DuckDB. Verify imports resolve from the
installed package rather than a source checkout.

To undo a first migration, restore the consumer's previous implementation or
commit, dependency/lockfile, and its tested scheduling configuration. Do not
delete recovery sets as part of rollback. Report unsupported filesystem behavior
or a failed verification with synthetic, non-secret diagnostics; use
[SECURITY.md](SECURITY.md) for a vulnerability.
