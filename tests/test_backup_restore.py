"""Tests for ``pm backup`` + ``pm restore`` and the underlying
:mod:`pollypm.backup` module.

Coverage target (from the issue spec):

1. ``pm backup`` creates a gzipped snapshot at the expected path.
2. Backup is a valid SQLite DB (can open + query).
3. Retention prunes older backups past ``--keep N``.
4. ``--full`` creates a tar.gz with the expected contents.
5. ``pm restore <path>`` without ``--confirm`` refuses + prints guidance.
6. ``pm restore <path> --confirm`` writes a .before-restore sibling
   and replaces live DB.
7. ``pm restore --dry-run`` prints what would happen without touching
   files.
8. Invalid snapshot path → error + exit 1.
9. Corrupt snapshot (not a valid SQLite) → error + exit 1.
"""

from __future__ import annotations

import gzip
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm import backup as backup_mod
from pollypm.cli import app as root_app


runner = CliRunner()


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


def _seed_state_db(path: Path) -> None:
    """Create a tiny SQLite DB with one row so snapshots have content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS probe (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT OR REPLACE INTO probe(k, v) VALUES (?, ?)", ("hello", "world"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A ``base_dir`` root that mimics ``~/.pollypm/``."""
    base = tmp_path / ".pollypm"
    base.mkdir()
    return base


@pytest.fixture
def state_db(fake_home: Path) -> Path:
    db = fake_home / "state.db"
    _seed_state_db(db)
    return db


@pytest.fixture
def pollypm_config(tmp_path: Path, fake_home: Path, state_db: Path) -> Path:
    """Minimal pollypm.toml that points at ``fake_home`` / ``state_db``.

    We keep ``tmux_session`` set to something invented so the soft
    probe in ``pm restore`` can't accidentally match a real session
    on the developer machine.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "PollyPM-Test"',
                'tmux_session = "pollypm-backup-test-session-does-not-exist"',
                f'workspace_root = "{tmp_path}"',
                f'base_dir = "{fake_home}"',
                f'logs_dir = "{fake_home}/logs"',
                f'snapshots_dir = "{fake_home}/snapshots"',
                f'state_db = "{state_db}"',
                "",
                "[pollypm]",
                'controller_account = "claude_test"',
                "",
                "[accounts.claude_test]",
                'provider = "claude"',
                'email = "test@example.com"',
                "",
            ]
        )
    )
    return config_path


def _invoke(*args: str) -> "CliRunner.invoke":  # type: ignore[name-defined]
    return runner.invoke(root_app, list(args))


def _backup(config_path: Path, *extra: str):
    return _invoke("backup", "--config", str(config_path), *extra)


def _restore(config_path: Path, snapshot: Path, *extra: str):
    return _invoke("restore", str(snapshot), "--config", str(config_path), *extra)


# --------------------------------------------------------------------- #
# 1 + 2. pm backup creates a gzipped snapshot; it's a valid SQLite DB.
# --------------------------------------------------------------------- #


def test_pm_backup_creates_gzipped_snapshot_at_expected_path(
    pollypm_config: Path, fake_home: Path
) -> None:
    result = _backup(pollypm_config)
    assert result.exit_code == 0, result.output
    backups_dir = fake_home / "backups"
    assert backups_dir.exists(), f"expected backup dir at {backups_dir}"
    snapshots = sorted(backups_dir.glob("state-db-*.db.gz"))
    assert len(snapshots) == 1, f"expected 1 snapshot, found {snapshots}"

    # Path name must be in the CLI summary.
    assert str(snapshots[0]) in result.output
    assert "DB size before:" in result.output
    assert "Archive size:" in result.output


def test_backup_is_a_valid_sqlite_db_after_decompression(
    pollypm_config: Path, fake_home: Path, tmp_path: Path
) -> None:
    result = _backup(pollypm_config)
    assert result.exit_code == 0, result.output

    snapshot = next((fake_home / "backups").glob("state-db-*.db.gz"))
    decompressed = tmp_path / "probe.db"
    with gzip.open(snapshot, "rb") as fin, decompressed.open("wb") as fout:
        fout.write(fin.read())

    conn = sqlite3.connect(decompressed)
    try:
        # Our seed row from the fixture must round-trip.
        row = conn.execute("SELECT v FROM probe WHERE k = ?", ("hello",)).fetchone()
        assert row == ("world",)
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# 3. Retention prunes older backups past --keep N.
# --------------------------------------------------------------------- #


def test_retention_prunes_older_backups_past_keep(
    pollypm_config: Path, fake_home: Path
) -> None:
    backups_dir = fake_home / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    # Seed 4 fake "old" snapshots with staggered mtimes so we can
    # reason about which ones survive. The newest `backup` run will
    # produce a 5th file, and with --keep 2 we expect only the 2
    # newest to remain.
    now = time.time()
    old_files: list[Path] = []
    for i in range(4):
        p = backups_dir / f"state-db-2020010{i}-010203.db.gz"
        p.write_bytes(b"not-a-real-backup")
        import os

        os.utime(p, (now - (100 - i), now - (100 - i)))
        old_files.append(p)

    result = _backup(pollypm_config, "--keep", "2")
    assert result.exit_code == 0, result.output

    remaining = sorted((fake_home / "backups").glob("state-db-*.db.gz"))
    assert len(remaining) == 2, f"expected 2 snapshots post-prune, got {remaining}"

    # The brand-new snapshot (mtime ~= now) is always among the
    # survivors. The second survivor is the youngest of the seeded
    # pre-existing files.
    assert old_files[3] in remaining or any(
        r.stat().st_mtime >= old_files[3].stat().st_mtime for r in remaining
    )
    # The two oldest seeded files must be gone.
    assert not old_files[0].exists()
    assert not old_files[1].exists()


# --------------------------------------------------------------------- #
# 4. --full creates a tar.gz with the expected contents.
# --------------------------------------------------------------------- #


def test_full_backup_creates_tar_gz_with_expected_contents(
    pollypm_config: Path, fake_home: Path
) -> None:
    # Drop a couple of files into the base_dir so we can assert they
    # come along for the ride.
    (fake_home / "logs").mkdir(exist_ok=True)
    (fake_home / "logs" / "heartbeat.log").write_text("tick\n")
    (fake_home / "snapshots").mkdir(exist_ok=True)
    (fake_home / "snapshots" / "snap.txt").write_text("frozen\n")

    result = _backup(pollypm_config, "--full")
    assert result.exit_code == 0, result.output

    archives = list((fake_home / "backups").glob("full-*.tar.gz"))
    assert len(archives) == 1, f"expected 1 full archive, got {archives}"

    with tarfile.open(archives[0], mode="r:gz") as tar:
        names = set(tar.getnames())

    # state.db is always at the archive root (restore looks for it there).
    assert "state.db" in names
    # Other base_dir contents are preserved under base/.
    assert any(n.startswith("base/logs") for n in names), names
    assert any(n.startswith("base/snapshots") for n in names), names
    # The backups/ subdir itself must NOT be inside the archive
    # (otherwise each run would grow quadratically).
    assert not any(n.startswith("base/backups") for n in names), names


# --------------------------------------------------------------------- #
# 5. pm restore without --confirm refuses.
# --------------------------------------------------------------------- #


def test_restore_without_confirm_refuses_and_prints_guidance(
    pollypm_config: Path, fake_home: Path
) -> None:
    backup_result = _backup(pollypm_config)
    assert backup_result.exit_code == 0, backup_result.output
    snapshot = next((fake_home / "backups").glob("state-db-*.db.gz"))

    result = _restore(pollypm_config, snapshot)
    assert result.exit_code == 1, result.output
    assert "refused" in result.output.lower() or "confirm" in result.output.lower()
    # Guidance must reference the --confirm flag and tmux kill-session.
    assert "--confirm" in result.output
    assert "tmux" in result.output or "pm reset" in result.output

    # No safety copy should have been written yet — we refused.
    assert not list(fake_home.glob("state.db.before-restore-*"))


# --------------------------------------------------------------------- #
# 6. pm restore --confirm writes a .before-restore sibling and replaces DB.
# --------------------------------------------------------------------- #


def test_restore_with_confirm_saves_safety_copy_and_replaces_db(
    pollypm_config: Path, fake_home: Path, state_db: Path
) -> None:
    # Take a clean baseline snapshot.
    backup_result = _backup(pollypm_config)
    assert backup_result.exit_code == 0, backup_result.output
    snapshot = next((fake_home / "backups").glob("state-db-*.db.gz"))

    # Mutate the live DB so we can tell the restore actually reverted it.
    conn = sqlite3.connect(state_db)
    try:
        conn.execute("UPDATE probe SET v = ? WHERE k = ?", ("mutated", "hello"))
        conn.commit()
    finally:
        conn.close()

    result = _restore(pollypm_config, snapshot, "--confirm")
    assert result.exit_code == 0, result.output

    # Safety sibling must exist and contain the MUTATED value (it is
    # a snapshot of the live DB *before* we restored).
    safeties = list(fake_home.glob("state.db.before-restore-*"))
    assert len(safeties) == 1, safeties
    safety_conn = sqlite3.connect(safeties[0])
    try:
        row = safety_conn.execute(
            "SELECT v FROM probe WHERE k = ?", ("hello",)
        ).fetchone()
        assert row == ("mutated",), "safety copy should hold pre-restore state"
    finally:
        safety_conn.close()

    # Live DB must now reflect the snapshot — back to "world".
    live_conn = sqlite3.connect(state_db)
    try:
        row = live_conn.execute(
            "SELECT v FROM probe WHERE k = ?", ("hello",)
        ).fetchone()
        assert row == ("world",), "live DB should have been rolled back"
    finally:
        live_conn.close()

    assert "Restored" in result.output
    assert str(safeties[0]) in result.output


# --------------------------------------------------------------------- #
# 7. pm restore --dry-run prints plan without touching files.
# --------------------------------------------------------------------- #


def test_restore_dry_run_does_not_touch_files(
    pollypm_config: Path, fake_home: Path, state_db: Path
) -> None:
    backup_result = _backup(pollypm_config)
    assert backup_result.exit_code == 0, backup_result.output
    snapshot = next((fake_home / "backups").glob("state-db-*.db.gz"))

    live_mtime_before = state_db.stat().st_mtime
    live_size_before = state_db.stat().st_size

    result = _restore(pollypm_config, snapshot, "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert str(snapshot) in result.output
    assert str(state_db) in result.output

    # No .before-restore safety copy should have been written.
    assert not list(fake_home.glob("state.db.before-restore-*"))
    # Live DB must be untouched.
    assert state_db.stat().st_mtime == live_mtime_before
    assert state_db.stat().st_size == live_size_before


# --------------------------------------------------------------------- #
# 8. Invalid snapshot path → error + exit 1.
# --------------------------------------------------------------------- #


def test_restore_nonexistent_snapshot_errors_and_exits_one(
    pollypm_config: Path, tmp_path: Path
) -> None:
    bogus = tmp_path / "does-not-exist.db.gz"
    result = _restore(pollypm_config, bogus, "--confirm")
    assert result.exit_code == 1, result.output
    assert "not found" in result.output.lower() or "failed" in result.output.lower()


# --------------------------------------------------------------------- #
# 9. Corrupt snapshot (not a valid SQLite) → error + exit 1.
# --------------------------------------------------------------------- #


def test_restore_corrupt_snapshot_errors_and_exits_one(
    pollypm_config: Path, tmp_path: Path
) -> None:
    # A .db.gz whose decompressed contents aren't SQLite at all.
    corrupt = tmp_path / "state-db-00000000-000000.db.gz"
    with gzip.open(corrupt, "wb") as fout:
        fout.write(b"this is not a sqlite database")

    result = _restore(pollypm_config, corrupt, "--confirm")
    assert result.exit_code == 1, result.output
    assert "valid" in result.output.lower() or "failed" in result.output.lower()


# --------------------------------------------------------------------- #
# Library-level checks that don't go through the CLI — these catch
# regressions in backup.py directly.
# --------------------------------------------------------------------- #


def test_backup_module_output_flag_writes_to_custom_path(
    fake_home: Path, state_db: Path, tmp_path: Path
) -> None:
    custom = tmp_path / "custom" / "mine.db.gz"
    result = backup_mod.backup_state_db(
        state_db,
        base_dir=fake_home,
        output=custom,
        full=False,
        keep=7,
    )
    assert result.path == custom
    assert custom.exists()


def test_backup_module_retention_prunes_only_default_dir(
    fake_home: Path, state_db: Path, tmp_path: Path
) -> None:
    # Seed many "old" snapshots in the default backup dir.
    backups_dir = fake_home / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    import os

    now = time.time()
    for i in range(10):
        p = backups_dir / f"state-db-2020010{i}-010203.db.gz"
        p.write_bytes(b"x")
        os.utime(p, (now - (1000 - i), now - (1000 - i)))

    result = backup_mod.backup_state_db(
        state_db, base_dir=fake_home, full=False, keep=3
    )
    assert len(result.pruned) >= 8  # 11 total pre-prune, minus 3 survivors
    remaining = list(backups_dir.glob("state-db-*.db.gz"))
    assert len(remaining) == 3


def test_plan_restore_rejects_full_archive_missing_state_db(
    fake_home: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "busted.tar.gz"
    with tarfile.open(broken, mode="w:gz") as tar:
        junk = tmp_path / "junk.txt"
        junk.write_text("hi")
        tar.add(junk, arcname="junk.txt")

    with pytest.raises(ValueError):
        backup_mod.plan_restore(broken, fake_home / "state.db")
