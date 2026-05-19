"""End-to-end pg_dump → pg_restore round-trip (issue #1737, Slice G).

Runs against the shared ``_pg_container`` fixture in
``tests/conftest_pg.py``. Each test seeds a per-case database, takes a
``pg_dump`` snapshot, restores it into a *fresh* database, and asserts
row-count parity.

Skipped automatically when neither Docker nor a local pg with
``vector`` is available — same gating as the other ``test_pg_*.py``
modules.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from pollypm import backup as backup_mod


# --------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------- #


def _require_pg_tools() -> tuple[str, str]:
    """Return absolute paths to ``pg_dump`` and ``pg_restore`` or skip.

    The container fixture guarantees a server side but the test
    runner's host may not have the client binaries (e.g. CI image
    without ``postgresql-client``). Skip rather than fail in that
    case so the broader suite stays green.
    """
    try:
        pg_dump = backup_mod._resolve_pg_binary("pg_dump")  # type: ignore[attr-defined]
        pg_restore = backup_mod._resolve_pg_binary("pg_restore")  # type: ignore[attr-defined]
    except backup_mod.PgBackupError:
        pytest.skip("pg_dump / pg_restore client tools not installed")
    return pg_dump, pg_restore


def _base_dsn_parts(dsn: str) -> tuple[str, str]:
    """Split ``postgresql://user:pw@host:port/dbname`` → (base, dbname)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    dbname = parts.path.lstrip("/") or "postgres"
    base = urlunsplit(parts._replace(path="/postgres"))
    return base, dbname


@pytest.fixture
def pg_dsn_pair(_pg_container) -> Iterator[tuple[str, str]]:
    """Yield ``(source_dsn, target_dsn)`` — two fresh DBs on the same server.

    Both are created before the test and dropped after. ``source_dsn``
    is seeded with sample rows; ``target_dsn`` is left empty so the
    restore has somewhere to land. Round-trip is then asserted at the
    test level.
    """
    import psycopg  # type: ignore[import-not-found]

    base_admin_dsn, _ = _base_dsn_parts(_pg_container)
    src_name = f"pollypm_backup_src_{uuid.uuid4().hex[:10]}"
    dst_name = f"pollypm_backup_dst_{uuid.uuid4().hex[:10]}"

    # CREATE DATABASE cannot run inside a transaction — use autocommit.
    with psycopg.connect(base_admin_dsn, autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{src_name}"')
            cur.execute(f'CREATE DATABASE "{dst_name}"')

    src_base, _ = _base_dsn_parts(_pg_container)
    src_dsn = src_base.replace("/postgres", f"/{src_name}")
    dst_dsn = src_base.replace("/postgres", f"/{dst_name}")
    try:
        yield src_dsn, dst_dsn
    finally:
        with psycopg.connect(base_admin_dsn, autocommit=True) as admin:
            with admin.cursor() as cur:
                for name in (src_name, dst_name):
                    # Force-disconnect any lingering sessions before drop.
                    cur.execute(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity WHERE datname = %s",
                        (name,),
                    )
                    cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _seed_source(dsn: str) -> dict[str, int]:
    """Seed the source DB with deterministic rows; return ``{table: count}``."""
    import psycopg  # type: ignore[import-not-found]

    counts = {"work_tasks": 5, "messages": 3}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE work_tasks ("
                " task_number int PRIMARY KEY,"
                " project text NOT NULL,"
                " title text NOT NULL"
                ")"
            )
            for i in range(counts["work_tasks"]):
                cur.execute(
                    "INSERT INTO work_tasks (task_number, project, title) "
                    "VALUES (%s, %s, %s)",
                    (i + 1, "demo", f"task-{i + 1}"),
                )
            cur.execute(
                "CREATE TABLE messages ("
                " id bigserial PRIMARY KEY,"
                " body text NOT NULL"
                ")"
            )
            for i in range(counts["messages"]):
                cur.execute(
                    "INSERT INTO messages (body) VALUES (%s)",
                    (f"msg-{i + 1}",),
                )
        conn.commit()
    return counts


def _row_counts(dsn: str, tables: list[str]) -> dict[str, int]:
    import psycopg  # type: ignore[import-not-found]

    out: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                row = cur.fetchone()
                out[table] = int(row[0]) if row else 0
    return out


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


def test_pg_dump_then_pg_restore_round_trip(
    pg_dsn_pair, tmp_path: Path, monkeypatch
) -> None:
    """pg_dump from source, pg_restore into target, verify row counts."""
    _require_pg_tools()
    src_dsn, dst_dsn = pg_dsn_pair
    expected = _seed_source(src_dsn)

    # Direct invocation — point ``resolve_dsn`` at the source for the
    # backup, then at the target for the restore. The simplest way is
    # via the env var override (highest priority in the resolver).
    monkeypatch.setenv("POLLYPM_PG_DSN", src_dsn)

    base_dir = tmp_path / ".pollypm"
    backup_result = backup_mod.backup_pg_dsn(base_dir=base_dir)

    assert backup_result.path.exists()
    assert backup_result.path.read_bytes()[:5] == b"PGDMP"
    assert backup_result.archive_size > 0

    # The audit index must record this one snapshot.
    index_path = base_dir / "backups" / "index.jsonl"
    assert index_path.exists()
    lines = index_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    # Now restore into the *target* DSN.
    monkeypatch.setenv("POLLYPM_PG_DSN", dst_dsn)
    plan = backup_mod.plan_pg_restore(backup_result.path)
    assert plan.dsn == dst_dsn

    # The safety dump runs against the (empty) target. That's fine —
    # pg_dump of an empty DB succeeds. We don't assert on the safety
    # file existing because the integration-test target DB starts empty
    # and the dump still has minimal content; either outcome is
    # acceptable.
    result = backup_mod.execute_pg_restore(plan)
    assert result.dsn == dst_dsn

    observed = _row_counts(dst_dsn, list(expected.keys()))
    assert observed == expected


def test_plan_pg_restore_rejects_non_pgdump(tmp_path: Path) -> None:
    """Files that don't start with the ``PGDMP`` magic must be refused."""
    junk = tmp_path / "junk.pgdump"
    junk.write_bytes(b"NOT_A_PG_DUMP")
    with pytest.raises(ValueError):
        backup_mod.plan_pg_restore(junk)


def test_plan_pg_restore_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_mod.plan_pg_restore(tmp_path / "missing.pgdump")


def test_pg_backup_records_audit_index(
    pg_dsn_pair, tmp_path: Path, monkeypatch
) -> None:
    """A pg backup must append a JSONL audit row to ``backups/index.jsonl``."""
    import json

    _require_pg_tools()
    src_dsn, _ = pg_dsn_pair
    _seed_source(src_dsn)

    monkeypatch.setenv("POLLYPM_PG_DSN", src_dsn)
    base_dir = tmp_path / ".pollypm"

    backup_mod.backup_pg_dsn(base_dir=base_dir)
    backup_mod.backup_pg_dsn(base_dir=base_dir)

    index_path = base_dir / "backups" / "index.jsonl"
    lines = index_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert record["event"] == "pg_backup"
        assert record["archive_size"] > 0
        assert record["path"].endswith(".pgdump")


def test_pg_backup_keep_prunes_older_snapshots(
    pg_dsn_pair, tmp_path: Path, monkeypatch
) -> None:
    """With ``keep=2`` and 4 snapshots, only the 2 newest survive."""
    import os
    import time

    _require_pg_tools()
    src_dsn, _ = pg_dsn_pair
    _seed_source(src_dsn)

    monkeypatch.setenv("POLLYPM_PG_DSN", src_dsn)
    base_dir = tmp_path / ".pollypm"

    # Take three snapshots up-front; bump the mtimes apart so the
    # oldest are clearly the first two.
    snaps: list[Path] = []
    for i in range(3):
        result = backup_mod.backup_pg_dsn(base_dir=base_dir, keep=99)
        snaps.append(result.path)
        os.utime(result.path, (time.time() - (3 - i) * 100,) * 2)

    final = backup_mod.backup_pg_dsn(base_dir=base_dir, keep=2)
    remaining = sorted(
        (base_dir / "backups").glob("pg-*.pgdump"),
        key=lambda p: p.stat().st_mtime,
    )
    assert len(remaining) == 2
    # The final snapshot must always survive.
    assert final.path in remaining
    # Two of the three earlier snapshots were pruned.
    assert len(final.pruned) == 2
