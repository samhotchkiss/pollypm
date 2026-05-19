"""Tests for ``pm storage migrate-to-pg`` (issue #1737, Slice E).

Strategy
--------

The migrator is exercised end-to-end against a real testcontainer pg
(or a local pg with pgvector if Docker isn't available) so the COPY
pipeline, idempotency table, and parity checks run against the actual
dialect. A pure-Python in-memory sqlite fixture stands in for the
workspace state.db — sqlite's behaviour is consistent enough across
platforms that an on-disk file isn't required for these cases.

Coverage checklist (per the slice spec):

* row-count parity post-copy ✅
* idempotency: second run is a no-op ✅
* dry-run prints summary but doesn't mutate pg ✅
* source files survive on failure ✅
* per-project DB project_key injection ✅
* legacy timestamp parsing (sqlite TEXT → pg timestamptz) ✅
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


# --------------------------------------------------------------------- #
# Sqlite fixture helpers
# --------------------------------------------------------------------- #


_WORK_TASKS_DDL = """
CREATE TABLE work_tasks (
    project TEXT NOT NULL,
    task_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '[]',
    work_status TEXT NOT NULL DEFAULT 'draft',
    flow_template_id TEXT NOT NULL,
    flow_template_version INTEGER NOT NULL DEFAULT 1,
    current_node_id TEXT,
    assignee TEXT,
    priority TEXT NOT NULL DEFAULT 'normal',
    requires_human_review INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    acceptance_criteria TEXT,
    constraints TEXT,
    relevant_files TEXT NOT NULL DEFAULT '[]',
    parent_project TEXT,
    parent_task_number INTEGER,
    supersedes_project TEXT,
    supersedes_task_number INTEGER,
    plan_version INTEGER NOT NULL DEFAULT 1,
    predecessor_task_id TEXT,
    kind TEXT NOT NULL DEFAULT 'legacy',
    roles TEXT NOT NULL DEFAULT '{}',
    external_refs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'test',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project, task_number)
);
"""


_MESSAGES_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    type TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'immediate',
    recipient TEXT NOT NULL,
    sender TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    parent_id INTEGER,
    subject TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    labels TEXT NOT NULL DEFAULT '[]',
    kind TEXT NOT NULL DEFAULT 'legacy',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT
);
"""


_HEARTBEATS_DDL = """
CREATE TABLE heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_name TEXT NOT NULL,
    tmux_window TEXT NOT NULL,
    pane_id TEXT NOT NULL,
    pane_command TEXT NOT NULL,
    pane_dead INTEGER NOT NULL,
    log_bytes INTEGER NOT NULL,
    snapshot_path TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def _build_sqlite_workspace(path: Path) -> None:
    """Seed a sqlite file with the minimal table shape the migrator
    needs to verify a non-trivial copy."""
    conn = sqlite3.connect(path)
    conn.executescript(_WORK_TASKS_DDL + _MESSAGES_DDL + _HEARTBEATS_DDL)
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """
        INSERT INTO work_tasks (
            project, task_number, title, type, labels, work_status,
            flow_template_id, roles, external_refs,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "demo", 1, "First task", "task",
                '["a", "b"]', "queued",
                "default",
                '{"worker": "alice"}',
                '{}',
                now, now,
            ),
            (
                "demo", 2, "Second task", "task",
                "[]", "in_progress",
                "default",
                '{"worker": "bob"}',
                '{"github": "#42"}',
                now, now,
            ),
            (
                "other", 1, "Other task", "task",
                "[]", "draft",
                "default",
                "{}", "{}",
                now, now,
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO messages (
            scope, type, recipient, sender, subject, body,
            payload_json, labels, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "demo", "notify", "user", "system",
                "Hello", "World", '{"k": "v"}', '["x"]',
                now, now,
            ),
            (
                "demo", "alert", "user", "system",
                "Alert!", "body", "{}", "[]", now, now,
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO heartbeats (
            session_name, tmux_window, pane_id, pane_command,
            pane_dead, log_bytes, snapshot_path, snapshot_hash,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("operator", "main", "%1", "claude", 0, 1024, "/tmp/snap1", "h1", now),
            ("operator", "main", "%2", "claude", 1, 0, "/tmp/snap2", "", now),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def sqlite_workspace(tmp_path: Path) -> Path:
    """Seeded sqlite workspace file at ``<tmp>/state.db``."""
    db = tmp_path / "state.db"
    _build_sqlite_workspace(db)
    return db


@pytest.fixture()
def per_project_sqlite(tmp_path: Path) -> Path:
    """A small per-project sqlite — only work_tasks under one project."""
    project_dir = tmp_path / "myproject" / ".pollypm"
    project_dir.mkdir(parents=True, exist_ok=True)
    db = project_dir / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(_WORK_TASKS_DDL)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO work_tasks (
            project, task_number, title, type, work_status,
            flow_template_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "myproject", 1, "Per-project legacy task", "task",
            "queued", "default", now, now,
        ),
    )
    conn.commit()
    conn.close()
    return db


# --------------------------------------------------------------------- #
# Pure-Python tests (no pg required)
# --------------------------------------------------------------------- #


def test_compute_sha256_is_stable(sqlite_workspace):
    from pollypm.storage.pg_migration_tool import compute_sha256

    h1 = compute_sha256(sqlite_workspace)
    h2 = compute_sha256(sqlite_workspace)
    assert h1 == h2
    assert len(h1) == 64


def test_discover_sources_returns_explicit_override(tmp_path, sqlite_workspace):
    from pollypm.storage.pg_migration_tool import discover_sources

    sources = discover_sources(
        config=None,
        include_legacy_per_project=False,
        from_sqlite_override=str(sqlite_workspace),
    )
    assert len(sources) == 1
    assert sources[0].path == sqlite_workspace.resolve()
    assert sources[0].kind == "workspace"


def test_discover_sources_skips_nonexistent_override(tmp_path):
    from pollypm.storage.pg_migration_tool import discover_sources

    sources = discover_sources(
        config=None,
        include_legacy_per_project=False,
        from_sqlite_override=str(tmp_path / "does-not-exist.db"),
    )
    assert sources == []


def test_discover_sources_auto_from_config(tmp_path, sqlite_workspace):
    """``auto`` mode must pick up the workspace DB and per-project DBs."""
    from pollypm.storage.pg_migration_tool import discover_sources

    # Build a minimal config shape that matches what ``load_config``
    # would return for the relevant attributes.
    class _Project:
        workspace_root = sqlite_workspace.parent

    proj_dir = sqlite_workspace.parent / "demo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / ".pollypm").mkdir(parents=True, exist_ok=True)
    per_project_db = proj_dir / ".pollypm" / "state.db"
    # Reuse the workspace DDL — content doesn't matter for discovery.
    sqlite3.connect(per_project_db).close()

    class _Known:
        path = proj_dir

    class _Config:
        project = _Project()
        projects = {"demo": _Known()}

    # Workspace DB is at <tmp>/state.db; auto-discovery looks for
    # <workspace_root>/.pollypm/state.db. Re-shape: put the seeded DB
    # inside a .pollypm subdir.
    workspace_pollypm = sqlite_workspace.parent / ".pollypm"
    workspace_pollypm.mkdir(parents=True, exist_ok=True)
    target = workspace_pollypm / "state.db"
    sqlite_workspace.rename(target)

    sources = discover_sources(
        config=_Config(),
        include_legacy_per_project=True,
        from_sqlite_override="auto",
    )
    paths = {s.path for s in sources}
    assert target.resolve() in paths
    assert per_project_db.resolve() in paths


def test_parse_iso_timestamp_handles_legacy_format():
    from pollypm.storage.pg_migration_tool import _parse_iso_timestamp

    # sqlite CURRENT_TIMESTAMP default form
    dt = _parse_iso_timestamp("2025-01-15 10:30:00")
    assert dt is not None
    assert dt.year == 2025 and dt.tzinfo is not None

    # Python isoformat with tz
    dt = _parse_iso_timestamp("2025-01-15T10:30:00+00:00")
    assert dt is not None and dt.year == 2025

    # Trailing Z
    dt = _parse_iso_timestamp("2025-01-15T10:30:00Z")
    assert dt is not None and dt.year == 2025

    assert _parse_iso_timestamp(None) is None
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("not a date") is None


def test_parse_json_text_handles_text_and_objects():
    from pollypm.storage.pg_migration_tool import _parse_json_text

    assert _parse_json_text('{"a": 1}', {}) == {"a": 1}
    assert _parse_json_text("[]", []) == []
    assert _parse_json_text("", {}) == {}
    assert _parse_json_text(None, []) == []
    assert _parse_json_text("not json", {"d": 1}) == {"d": 1}
    assert _parse_json_text({"already": "parsed"}, {}) == {"already": "parsed"}


# --------------------------------------------------------------------- #
# Pg-backed tests
# --------------------------------------------------------------------- #


@pytest.fixture()
def applied_pg_pool(pg_schema_pool):
    """Apply the canonical pg schema to the test pool so the migrator's
    pre-flight check passes and the destination tables exist."""
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)
    return pg_schema_pool


def test_preflight_ok_after_schema_applied(applied_pg_pool):
    from pollypm.storage.pg_migration_tool import preflight

    result = preflight(applied_pg_pool)
    assert result.ok, result.message
    assert result.vector_installed
    assert result.migration_version == 1


def test_dry_run_does_not_mutate_pg(applied_pg_pool, sqlite_workspace):
    """Dry-run must surface a full report but leave pg untouched."""
    from pollypm.storage.pg_migration_tool import (
        SourceDescriptor,
        migrate_sources,
    )

    sources = [
        SourceDescriptor(
            path=sqlite_workspace, kind="workspace", project_key=""
        )
    ]
    run = migrate_sources(sources, pool=applied_pg_pool, commit=False)
    assert run.dry_run is True
    assert run.committed is False
    assert len(run.sources) == 1
    report = run.sources[0]
    assert report.failure is None
    # The copy ran but was rolled back — pg side must be empty.
    with applied_pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_tasks")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM _pg_migration_audit")
        assert cur.fetchone()[0] == 0
    # Source file must survive a dry-run.
    assert sqlite_workspace.exists()


def test_commit_copies_rows_and_renames_source(
    applied_pg_pool, sqlite_workspace
):
    """Happy-path commit: rows land, parity holds, source is renamed."""
    from pollypm.storage.pg_migration_tool import (
        SourceDescriptor,
        migrate_sources,
    )

    sources = [
        SourceDescriptor(
            path=sqlite_workspace, kind="workspace", project_key=""
        )
    ]
    run = migrate_sources(sources, pool=applied_pg_pool, commit=True)
    assert run.committed is True
    assert len(run.sources) == 1
    report = run.sources[0]
    assert report.failure is None, report.failure
    assert report.completed_at is not None
    assert report.renamed_to is not None
    assert report.renamed_to.exists()
    assert not sqlite_workspace.exists()

    # Verify the rows actually landed.
    with applied_pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_tasks")
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT count(*) FROM messages")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM heartbeats")
        assert cur.fetchone()[0] == 2
        # jsonb round-trip
        cur.execute(
            "SELECT labels FROM work_tasks "
            "WHERE project = 'demo' AND task_number = 1"
        )
        labels = cur.fetchone()[0]
        assert labels == ["a", "b"]
        # boolean round-trip (pane_dead 0/1 → bool)
        cur.execute("SELECT pane_dead FROM heartbeats ORDER BY pane_id")
        rows = [r[0] for r in cur.fetchall()]
        assert rows == [False, True]
        # audit row exists
        cur.execute(
            "SELECT count(*) FROM _pg_migration_audit "
            "WHERE source_sha256 = %s",
            (report.source_sha256,),
        )
        assert cur.fetchone()[0] == 1


def test_idempotency_second_run_is_noop(applied_pg_pool, sqlite_workspace):
    """Running the migration twice on the same SHA must skip cleanly."""
    from pollypm.storage.pg_migration_tool import (
        SourceDescriptor,
        compute_sha256,
        migrate_sources,
    )

    # SHA must be stable across the rename — the audit table keys on
    # content hash, not path, so renaming the source DB must not break
    # idempotency. Capture both before/after and assert equality.
    sha_before = compute_sha256(sqlite_workspace)
    sources = [
        SourceDescriptor(
            path=sqlite_workspace, kind="workspace", project_key=""
        )
    ]
    # First run commits + renames.
    run1 = migrate_sources(sources, pool=applied_pg_pool, commit=True)
    assert run1.sources[0].failure is None
    renamed = run1.sources[0].renamed_to
    assert renamed is not None and renamed.exists()
    assert compute_sha256(renamed) == sha_before

    # Build a "fresh" source descriptor pointing at the renamed file —
    # the SHA is identical so the audit table should short-circuit.
    sources2 = [
        SourceDescriptor(path=renamed, kind="workspace", project_key="")
    ]
    run2 = migrate_sources(sources2, pool=applied_pg_pool, commit=True)
    assert len(run2.sources) == 1
    skip = run2.sources[0]
    assert skip.skipped_already_imported_at is not None
    assert skip.failure is None
    # And pg row counts didn't double.
    with applied_pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_tasks")
        assert cur.fetchone()[0] == 3


def test_per_project_db_injects_project_key(
    applied_pg_pool, per_project_sqlite
):
    """A per-project source must surface the derived project_key on rows
    that don't carry it natively."""
    from pollypm.storage.pg_migration_tool import (
        SourceDescriptor,
        migrate_sources,
    )

    sources = [
        SourceDescriptor(
            path=per_project_sqlite,
            kind="per_project",
            project_key="myproject",
        )
    ]
    run = migrate_sources(sources, pool=applied_pg_pool, commit=True)
    report = run.sources[0]
    assert report.failure is None, report.failure
    with applied_pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project, project_key FROM work_tasks "
            "WHERE project = 'myproject'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "myproject"
        assert row[1] == "myproject"


def test_failure_preserves_source_and_does_not_audit(
    applied_pg_pool, sqlite_workspace, monkeypatch
):
    """If any single table copy raises, the whole txn rolls back and
    the source sqlite file is NOT renamed."""
    from pollypm.storage import pg_migration_tool
    from pollypm.storage.pg_migration_tool import (
        SourceDescriptor,
        migrate_sources,
    )

    original = pg_migration_tool._copy_table

    def _boom(*, conn, sqlite_conn, spec, project_key, batch_size=5000):
        if spec.sqlite_table == "messages":
            raise RuntimeError("simulated failure on messages")
        return original(
            conn=conn,
            sqlite_conn=sqlite_conn,
            spec=spec,
            project_key=project_key,
            batch_size=batch_size,
        )

    monkeypatch.setattr(pg_migration_tool, "_copy_table", _boom)

    sources = [
        SourceDescriptor(
            path=sqlite_workspace, kind="workspace", project_key=""
        )
    ]
    run = migrate_sources(sources, pool=applied_pg_pool, commit=True)
    report = run.sources[0]
    assert report.failure is not None
    assert "messages" in (report.failed_table or "")
    # Source still on disk — caller can retry after fixing the cause.
    assert sqlite_workspace.exists()
    assert report.renamed_to is None

    # Verify rollback: no rows in pg, no audit row.
    with applied_pg_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_tasks")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM _pg_migration_audit")
        assert cur.fetchone()[0] == 0


def test_format_run_summary_includes_per_source_status(
    applied_pg_pool, sqlite_workspace
):
    """The CLI-facing summary string must mention each source's status."""
    from pollypm.storage.pg_migration_tool import (
        SourceDescriptor,
        format_run_summary,
        migrate_sources,
    )

    sources = [
        SourceDescriptor(
            path=sqlite_workspace, kind="workspace", project_key=""
        )
    ]
    run = migrate_sources(sources, pool=applied_pg_pool, commit=False)
    summary = format_run_summary(run)
    assert "DRY RUN" in summary
    assert str(sqlite_workspace) in summary
    # Dry-run mentions the rolled-back state.
    assert "untouched" in summary or "dry-run" in summary.lower()
