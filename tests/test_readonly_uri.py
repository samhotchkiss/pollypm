"""Tests for #1674 — read-only SQLite URI escaping.

Before the fix the various ``file:{path}?mode=ro`` callsites in
``storage/`` (and the non-storage callers added by #1561 / #1617 /
#1625 / #1658) embedded the workspace path into the URI without
percent-encoding it. SQLite parses ``#`` as a URI fragment and ``?``
as the start of the query string, so any workspace whose path
contained those characters (or spaces, ``%``, etc.) silently failed
to open and the presentation-side facades returned ``None`` instead
of the real aggregate.

This module pins two things:

1. ``pollypm.storage.sqlite_pragmas.readonly_uri`` percent-encodes
   metacharacters but leaves ``/`` as a literal separator so SQLite's
   URI parser still sees a normal absolute path.
2. The high-traffic storage facades that previously broke under
   metacharacter paths now return real rows.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from pollypm.storage.doctor_state_probes import count_work_tasks_ro
from pollypm.storage.legacy_per_project_db import _open_ro
from pollypm.storage.project_state_purge import count_project_state_rows
from pollypm.storage.sqlite_pragmas import readonly_uri
from pollypm.storage.work_session_queries import (
    aggregate_project_session_tokens,
)
from pollypm.storage.work_task_state import project_task_total_fast


def test_readonly_uri_escapes_hash(tmp_path: Path) -> None:
    raw = tmp_path / "with#hash.db"
    uri = readonly_uri(raw)
    assert "%23" in uri
    assert "#" not in uri.split("?", 1)[0]
    assert uri.endswith("?mode=ro")


def test_readonly_uri_escapes_question_mark(tmp_path: Path) -> None:
    raw = tmp_path / "with?query.db"
    uri = readonly_uri(raw)
    # Only one ``?`` remains (the one that starts ``mode=ro``).
    assert uri.count("?") == 1
    assert "%3F" in uri


def test_readonly_uri_keeps_slash_unencoded(tmp_path: Path) -> None:
    raw = tmp_path / "ok.db"
    uri = readonly_uri(raw)
    # The path part should still contain literal ``/`` separators.
    path_part = uri.removeprefix("file:").split("?", 1)[0]
    assert "/" in path_part
    assert "%2F" not in path_part


def test_readonly_uri_immutable_flag(tmp_path: Path) -> None:
    raw = tmp_path / "ok.db"
    uri = readonly_uri(raw, immutable=True)
    assert uri.endswith("?mode=ro&immutable=1")


def test_readonly_uri_accepts_path_with_space(tmp_path: Path) -> None:
    raw = tmp_path / "with space.db"
    uri = readonly_uri(raw)
    assert "%20" in uri
    assert " " not in uri


def _seed_state_db(db: Path, *, project: str = "alpha") -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE work_sessions ("
            "  task_project TEXT, "
            "  total_input_tokens INTEGER, "
            "  total_output_tokens INTEGER"
            ")"
        )
        conn.execute(
            "INSERT INTO work_sessions VALUES (?, ?, ?)",
            (project, 100, 50),
        )
        conn.execute(
            "CREATE TABLE work_tasks ("
            "  project TEXT, task_number INTEGER, work_status TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO work_tasks VALUES (?, 1, 'todo')", (project,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "subdir",
    [
        "with#hash",
        "with?query",
        "with space",
        "mix#and?both",
    ],
)
def test_work_session_aggregate_handles_metacharacter_paths(
    tmp_path: Path,
    subdir: str,
) -> None:
    """#1674 regression — pre-fix returned ``None`` for these paths."""
    root = tmp_path / subdir
    root.mkdir()
    db = root / "state.db"
    _seed_state_db(db)

    result = aggregate_project_session_tokens(db, project_key="alpha")
    assert result == (100, 50)


def test_doctor_probe_handles_metacharacter_paths(tmp_path: Path) -> None:
    """#1674 regression — doctor probes also embedded the raw path."""
    root = tmp_path / "with#hash?query"
    root.mkdir()
    db = root / "state.db"
    _seed_state_db(db)

    assert count_work_tasks_ro(db) == 1


def test_work_task_state_fast_count_handles_metacharacter_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "with#hash"
    root.mkdir()
    db = root / "state.db"
    _seed_state_db(db)

    assert project_task_total_fast(db, project_key="alpha") == 1


def test_legacy_per_project_open_ro_handles_metacharacter_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "with?query"
    root.mkdir()
    db = root / "state.db"
    _seed_state_db(db)

    conn = _open_ro(db)
    assert conn is not None
    try:
        row = conn.execute("SELECT COUNT(*) FROM work_tasks").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    "subdir",
    [
        "with#hash",
        "with?query",
        "mix#and?both",
    ],
)
def test_project_state_purge_count_handles_metacharacter_paths(
    tmp_path: Path,
    subdir: str,
) -> None:
    """#1691 regression — ``count_project_state_rows`` is the gate for
    ``pm project remove --purge-state``; if the read-only probe returns
    zero rows because the workspace path contains ``#``/``?``, the CLI
    skips the actual DELETE and silently leaves orphaned rows behind.
    """
    root = tmp_path / subdir
    root.mkdir()
    db = root / "state.db"
    _seed_state_db(db, project="demo")

    counts = count_project_state_rows(db, "demo")
    assert counts["work_tasks"] == 1
