"""Regression tests for #1652 — read-only aggregate against ``work_sessions``.

``pollypm.storage.work_session_queries.aggregate_project_session_tokens``
is documented as a read-only storage facade used by the render-side
``cockpit_sections`` Tokens line. Prior to #1652 it opened the workspace
DB in the default read-write mode and applied the normal workspace
pragmas (``PRAGMA journal_mode=WAL`` etc.), which silently mutated the
DB's journal mode and weakened the read-only boundary that #1376
introduced.

These tests pin the fix:

* The aggregate succeeds when only a ``file:<path>?mode=ro`` URI is
  available (e.g. file permissions deny writes).
* The aggregate does not flip a freshly-created rollback-journal DB
  into WAL mode (read-only callers must not mutate the writer's
  journal-mode contract).
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from pollypm.storage.work_session_queries import (
    aggregate_project_session_tokens,
)


def _seed_work_sessions_db(db_path: Path) -> None:
    """Create a minimal ``work_sessions`` table with two rows."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE work_sessions ("
            "  task_project TEXT, "
            "  total_input_tokens INTEGER, "
            "  total_output_tokens INTEGER"
            ")"
        )
        conn.executemany(
            "INSERT INTO work_sessions "
            "(task_project, total_input_tokens, total_output_tokens) "
            "VALUES (?, ?, ?)",
            [
                ("alpha", 100, 50),
                ("alpha", 200, 25),
                ("beta", 999, 999),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _journal_mode(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""
    finally:
        conn.close()


def test_aggregate_returns_sums_for_project(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_work_sessions_db(db)

    result = aggregate_project_session_tokens(db, project_key="alpha")
    assert result == (300, 75)


def test_aggregate_does_not_flip_journal_mode_to_wal(tmp_path: Path) -> None:
    """#1652 — read path must not mutate the writer's journal-mode contract.

    A freshly-created SQLite DB defaults to rollback journal (``delete``).
    The pre-fix code applied ``apply_workspace_pragmas(conn)`` which
    persistently flipped the DB into WAL mode from a read-only callsite.
    """

    db = tmp_path / "state.db"
    _seed_work_sessions_db(db)

    before = _journal_mode(db)
    assert before != "wal", (
        "test precondition: fresh DB should not already be in WAL mode "
        f"(got {before!r})"
    )

    aggregate_project_session_tokens(db, project_key="alpha")

    after = _journal_mode(db)
    assert after == before, (
        f"aggregate mutated journal_mode from {before!r} to {after!r} — "
        "read-only callers must not flip WAL"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX chmod read-only semantics not portable to Windows",
)
def test_aggregate_works_against_read_only_file(tmp_path: Path) -> None:
    """#1652 — aggregate must succeed when the DB file is read-only.

    Pre-fix this raised ``sqlite3.OperationalError: attempt to write a
    readonly database`` (or silently no-op) because the default
    ``sqlite3.connect`` opens in read-write mode and the workspace
    pragma helper tries to set ``journal_mode=WAL`` on connect. Post-fix
    the function opens via ``file:<path>?mode=ro`` and skips the
    mode-flip pragma, so a chmod-locked file still returns sums.
    """

    db = tmp_path / "state.db"
    _seed_work_sessions_db(db)

    # Strip write bits from the file (and any WAL/SHM sidecars that may
    # exist) so a read-write connect would fail at first write.
    original_mode = db.stat().st_mode
    db.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    # Also drop write on the parent dir so SQLite can't create new
    # sidecars on open.
    parent_mode = tmp_path.stat().st_mode
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = aggregate_project_session_tokens(db, project_key="alpha")
        assert result == (300, 75)
    finally:
        # Restore so pytest's tmp_path cleanup can remove the tree.
        os.chmod(tmp_path, parent_mode)
        db.chmod(original_mode)


def test_aggregate_returns_none_when_db_missing(tmp_path: Path) -> None:
    result = aggregate_project_session_tokens(
        tmp_path / "does-not-exist.db", project_key="alpha",
    )
    assert result is None


def test_aggregate_returns_none_when_table_missing(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    # Create an empty DB without the ``work_sessions`` table.
    sqlite3.connect(str(db)).close()

    result = aggregate_project_session_tokens(db, project_key="alpha")
    assert result is None
