"""Tests for the backup-backend dispatch shim (issue #1737, Slice G).

Coverage target:

1. ``backup_via_config`` routes to the pg branch and produces a
   ``PgBackupResult``. The pg_dump invocation itself is monkeypatched
   so the test runs without a live pg instance.
2. ``--full`` against a pg backend raises ``PgBackupError`` rather
   than silently dropping the flag.
3. ``restore_via_config`` returns the right plan type per backend.

These tests run without Docker — they exercise the dispatch logic and
the pg branch's filesystem side effects with the actual ``pg_dump``
binary stubbed. The end-to-end round-trip lives in
``tests/test_pg_backup.py``.

Slice K migration: dropped the sqlite branch of every parametrization.
The sqlite path is being deleted; only the pg dispatch remains.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pollypm import backup as backup_mod
from pollypm.models import (
    AccountConfig,
    EmbeddingSettings,
    PgStorageSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProjectSettings,
    ProviderKind,
    StorageSettings,
)


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


def _build_config(
    *,
    base_dir: Path,
    state_db: Path,
    storage_url: str = "postgresql://localhost:5432/pollypm-dispatch-test",
) -> PollyPMConfig:
    project = ProjectSettings(
        name="PollyPM-Test",
        tmux_session="pollypm-backup-dispatch-test",
        workspace_root=base_dir.parent,
        base_dir=base_dir,
        logs_dir=base_dir / "logs",
        snapshots_dir=base_dir / "snapshots",
        state_db=state_db,
    )
    pollypm = PollyPMSettings(controller_account="claude_test")
    accounts = {
        "claude_test": AccountConfig(
            name="claude_test",
            provider=ProviderKind("claude"),
            email="test@example.com",
        )
    }
    storage = StorageSettings(
        backend="postgres",
        url=storage_url,
        pg=PgStorageSettings(),
        embedding=EmbeddingSettings(),
    )
    return PollyPMConfig(
        project=project,
        pollypm=pollypm,
        accounts=accounts,
        sessions={},
        storage=storage,
    )


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    base = tmp_path / ".pollypm"
    base.mkdir()
    return base


@pytest.fixture
def state_db(fake_home: Path) -> Path:
    # Empty placeholder file — pg backend doesn't read this, but the
    # config dataclass requires a path.
    db = fake_home / "state.db"
    db.touch()
    return db


@pytest.fixture
def pg_config(fake_home: Path, state_db: Path) -> PollyPMConfig:
    return _build_config(base_dir=fake_home, state_db=state_db)


# --------------------------------------------------------------------- #
# Backup dispatch
# --------------------------------------------------------------------- #


def test_backup_via_config_postgres_returns_pg_result(
    pg_config: PollyPMConfig, fake_home: Path, monkeypatch
) -> None:
    """The pg branch must hit pg_dump, write a .pgdump file, and audit."""

    invocations: list[list[str]] = []

    def fake_run_pg_dump(dsn: str, dest: Path, *, pg_dump_binary=None) -> None:
        invocations.append([dsn, str(dest)])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PGDMP\x00fake-archive-payload")

    monkeypatch.setattr(backup_mod, "_run_pg_dump", fake_run_pg_dump)

    result = backup_mod.backup_via_config(pg_config)
    assert isinstance(result, backup_mod.PgBackupResult)
    assert result.path.exists()
    assert result.path.name.startswith("pg-")
    assert result.path.name.endswith(".pgdump")
    assert result.dsn == "postgresql://localhost:5432/pollypm-dispatch-test"
    assert invocations, "pg_dump shim must have been invoked"
    dsn_used, dest_used = invocations[0]
    assert dsn_used == result.dsn
    assert dest_used == str(result.path)

    index_path = fake_home / "backups" / "index.jsonl"
    assert index_path.exists()
    lines = index_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "pg_backup"
    assert record["path"] == str(result.path)
    assert record["archive_size"] == result.archive_size
    assert "duration_seconds" in record


def test_backup_via_config_full_against_postgres_raises(
    pg_config: PollyPMConfig,
) -> None:
    with pytest.raises(backup_mod.PgBackupError):
        backup_mod.backup_via_config(pg_config, full=True)


def test_backup_via_config_postgres_rotates_keep(
    pg_config: PollyPMConfig, fake_home: Path, monkeypatch
) -> None:
    """pg snapshots beyond --keep N must be pruned."""

    backups_dir = fake_home / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    # Seed three pre-existing snapshots with staggered mtimes so the
    # ordering is deterministic. The next backup will be the 4th —
    # with keep=2 we expect only the 2 newest to remain.
    import time

    now = time.time()
    old_paths: list[Path] = []
    for i in range(3):
        p = backups_dir / f"pg-old-{i}.pgdump"
        p.write_bytes(b"PGDMP\x00")
        mtime = now - (3 - i) * 100
        import os

        os.utime(p, (mtime, mtime))
        old_paths.append(p)

    def fake_run_pg_dump(dsn: str, dest: Path, *, pg_dump_binary=None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PGDMP\x00fresh")

    monkeypatch.setattr(backup_mod, "_run_pg_dump", fake_run_pg_dump)

    result = backup_mod.backup_via_config(pg_config, keep=2)
    assert isinstance(result, backup_mod.PgBackupResult)
    remaining = sorted(p.name for p in backups_dir.glob("pg-*.pgdump"))
    # 2 kept: the newest seeded old (pg-old-2.pgdump) + the fresh one.
    assert len(remaining) == 2
    assert result.path.name in remaining
    pruned_names = sorted(p.name for p in result.pruned)
    # The two oldest must have been pruned.
    assert "pg-old-0.pgdump" in pruned_names
    assert "pg-old-1.pgdump" in pruned_names


# --------------------------------------------------------------------- #
# Restore dispatch
# --------------------------------------------------------------------- #


def test_restore_via_config_postgres_returns_pg_plan(
    pg_config: PollyPMConfig, tmp_path: Path
) -> None:
    snapshot = tmp_path / "pg-fake.pgdump"
    # The first 5 bytes must be "PGDMP" for the planner to accept it.
    snapshot.write_bytes(b"PGDMP" + b"\x00" * 16)

    plan, backend = backup_mod.restore_via_config(pg_config, snapshot)
    assert backend == "postgres"
    assert isinstance(plan, backup_mod.PgRestorePlan)
    assert plan.snapshot_path == snapshot
    assert plan.dsn == "postgresql://localhost:5432/pollypm-dispatch-test"


def test_restore_via_config_postgres_rejects_non_pgdump(
    pg_config: PollyPMConfig, tmp_path: Path
) -> None:
    snapshot = tmp_path / "not-a-pgdump.bin"
    snapshot.write_bytes(b"NOT_PGDMP_HEADER")
    with pytest.raises(ValueError):
        backup_mod.restore_via_config(pg_config, snapshot)


def test_restore_via_config_missing_snapshot(
    pg_config: PollyPMConfig, tmp_path: Path
) -> None:
    snapshot = tmp_path / "does-not-exist.pgdump"
    with pytest.raises(FileNotFoundError):
        backup_mod.restore_via_config(pg_config, snapshot)


# --------------------------------------------------------------------- #
# latest_pg_backup_age_seconds — the doctor-check primitive
# --------------------------------------------------------------------- #


def test_latest_pg_backup_age_none_when_no_snapshots(fake_home: Path) -> None:
    assert backup_mod.latest_pg_backup_age_seconds(fake_home) is None


def test_latest_pg_backup_age_returns_seconds(fake_home: Path) -> None:
    import os
    import time

    backups_dir = fake_home / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    snap = backups_dir / "pg-recent.pgdump"
    snap.write_bytes(b"PGDMP\x00")
    five_minutes_ago = time.time() - 300
    os.utime(snap, (five_minutes_ago, five_minutes_ago))

    age = backup_mod.latest_pg_backup_age_seconds(fake_home)
    assert age is not None
    assert 290 <= age <= 320


# --------------------------------------------------------------------- #
# _resolve_pg_binary — fallback to pg_config --bindir
# --------------------------------------------------------------------- #


def test_resolve_pg_binary_raises_when_missing(monkeypatch) -> None:
    """If neither PATH nor pg_config can find pg_dump, raise PgBackupError."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(backup_mod.PgBackupError):
        backup_mod._resolve_pg_binary("pg_dump")
