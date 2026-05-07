"""Tests for work service SQLite schema."""

from __future__ import annotations

import sqlite3

import pytest

from pollypm.work.schema import create_work_tables


@pytest.fixture()
def conn():
    """In-memory SQLite connection."""
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

EXPECTED_TABLES = [
    "work_flow_templates",
    "work_flow_nodes",
    "work_tasks",
    "work_task_dependencies",
    "work_node_executions",
    "work_context_entries",
    "work_transitions",
]


class TestCreateWorkTables:
    def test_all_tables_created(self, conn):
        create_work_tables(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall()]
        for expected in EXPECTED_TABLES:
            assert expected in tables, f"Missing table: {expected}"

    def test_idempotent(self, conn):
        """Running create_work_tables twice must not raise."""
        create_work_tables(conn)
        create_work_tables(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cur.fetchall()]
        for expected in EXPECTED_TABLES:
            assert expected in tables


# ---------------------------------------------------------------------------
# Column checks
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return {column_name: type} for a table."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2] for row in cur.fetchall()}


class TestWorkTasksColumns:
    def test_primary_key_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        assert "project" in cols
        assert "task_number" in cols

    def test_state_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        for col in (
            "work_status",
            "flow_template_id",
            "current_node_id",
            "assignee",
            "priority",
            "requires_human_review",
        ):
            assert col in cols, f"Missing column: {col}"

    def test_content_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        for col in ("description", "acceptance_criteria", "constraints", "relevant_files"):
            assert col in cols, f"Missing column: {col}"

    def test_relationship_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        for col in ("parent_project", "parent_task_number", "supersedes_project", "supersedes_task_number"):
            assert col in cols, f"Missing column: {col}"

    def test_plan_metadata_columns(self, conn):
        """#1398 — plan_version + predecessor_task_id present on fresh DB."""
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        assert "plan_version" in cols
        assert "predecessor_task_id" in cols

    def test_audit_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        for col in ("created_at", "created_by", "updated_at"):
            assert col in cols, f"Missing column: {col}"

    def test_no_owner_or_blocked_stored(self, conn):
        """owner and blocked are derived -- they must NOT be stored columns."""
        create_work_tables(conn)
        cols = _columns(conn, "work_tasks")
        assert "owner" not in cols
        assert "blocked" not in cols


class TestFlowTemplatesColumns:
    def test_expected_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_flow_templates")
        for col in ("name", "version", "description", "roles", "start_node", "is_current", "created_at"):
            assert col in cols, f"Missing column: {col}"


class TestFlowNodesColumns:
    def test_expected_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_flow_nodes")
        for col in (
            "flow_template_name",
            "flow_template_version",
            "node_id",
            "name",
            "type",
            "actor_type",
            "actor_role",
            "next_node_id",
            "reject_node_id",
            "gates",
        ):
            assert col in cols, f"Missing column: {col}"


class TestNodeExecutionsColumns:
    def test_expected_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_node_executions")
        for col in (
            "task_project",
            "task_number",
            "node_id",
            "visit",
            "status",
            "work_output",
            "decision",
            "decision_reason",
            "started_at",
            "completed_at",
        ):
            assert col in cols, f"Missing column: {col}"


class TestDependenciesColumns:
    def test_expected_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_task_dependencies")
        for col in (
            "from_project",
            "from_task_number",
            "to_project",
            "to_task_number",
            "kind",
            "created_at",
        ):
            assert col in cols, f"Missing column: {col}"


class TestContextEntriesColumns:
    def test_expected_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_context_entries")
        for col in ("task_project", "task_number", "actor", "text", "created_at"):
            assert col in cols, f"Missing column: {col}"


class TestTransitionsColumns:
    def test_expected_columns(self, conn):
        create_work_tables(conn)
        cols = _columns(conn, "work_transitions")
        for col in (
            "task_project",
            "task_number",
            "from_state",
            "to_state",
            "actor",
            "reason",
            "created_at",
        ):
            assert col in cols, f"Missing column: {col}"


# ---------------------------------------------------------------------------
# Index existence
# ---------------------------------------------------------------------------


def _indexes(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    return {row[0] for row in cur.fetchall()}


def _index_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
        (name,),
    ).fetchone()
    assert row is not None and row[0] is not None
    return row[0]


class TestIndexes:
    def test_key_indexes_exist(self, conn):
        create_work_tables(conn)
        idxs = _indexes(conn)
        expected = {
            "idx_work_tasks_status",
            "idx_work_tasks_project_status",
            "idx_work_tasks_assignee",
            "idx_work_tasks_active",
            "idx_work_tasks_priority",
            "idx_work_deps_to",
            "idx_work_deps_from",
            "idx_work_exec_task",
            "idx_work_context_task",
            "idx_work_transitions_task",
        }
        for name in expected:
            assert name in idxs, f"Missing index: {name}"

    def test_active_tasks_index_is_partial(self, conn):
        create_work_tables(conn)
        sql = _index_sql(conn, "idx_work_tasks_active")
        assert "current_node_id" in sql
        assert "WHERE current_node_id IS NOT NULL" in sql


def test_legacy_db_gets_hot_query_indexes_and_schema_bump(conn):
    conn.executescript(
        """
        CREATE TABLE work_schema_version (
            version INTEGER NOT NULL,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO work_schema_version (version, description, applied_at)
        VALUES (4, 'old', '2026-01-01T00:00:00+00:00');
        """
    )

    create_work_tables(conn)

    idxs = _indexes(conn)
    assert "idx_work_tasks_active" in idxs
    assert "idx_work_deps_from" in idxs

    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM work_schema_version"
    ).fetchone()[0]
    # Migration 8 (#1398) records the plan_version + predecessor_task_id
    # column bumps on work_tasks so plan-task evolution metadata
    # surfaces on legacy DBs without a backfill pass.
    assert version == 8


def test_migration_6_adds_provider_columns_to_work_sessions(conn):
    """#809: legacy DBs without the provider columns get them on migrate."""
    create_work_tables(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(work_sessions)")}
    assert "provider" in cols
    assert "provider_home" in cols


def test_migration_7_adds_kickoff_sent_at_to_work_node_executions(conn):
    """#922: legacy DBs without ``kickoff_sent_at`` get it on migrate."""
    # Build a v6-shaped DB so the migration walk has to apply v7.
    conn.executescript(
        """
        CREATE TABLE work_schema_version (
            version INTEGER NOT NULL,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO work_schema_version (version, description, applied_at)
        VALUES (6, 'pre-#922', '2026-01-01T00:00:00+00:00');
        """
    )
    create_work_tables(conn)

    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(work_node_executions)")
    }
    assert "kickoff_sent_at" in cols
    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM work_schema_version"
    ).fetchone()[0]
    # The migration walk applies every pending step in order, so a v6
    # legacy DB ends up at the latest version (v8 after #1398) — not
    # at v7. Asserting the whole walk completed protects future
    # migrations from a stale floor here.
    assert version == 8


def test_migration_8_adds_plan_metadata_columns_to_work_tasks(conn):
    """#1398: legacy work_tasks rows get plan_version + predecessor_task_id.

    Builds a v7-shaped DB with a populated row that pre-dates the
    columns, runs the migration, and asserts:
      * the new columns exist after migration,
      * existing rows default to ``plan_version=1`` and
        ``predecessor_task_id=NULL`` (additive migration — no data
        loss / shape change),
      * the schema_version row bumps to v8.
    """
    # Create a v7-shaped work_tasks table (no plan_version /
    # predecessor_task_id columns) plus the schema_version row that
    # pins us to v7 so the migration walk has to apply v8.
    conn.executescript(
        """
        CREATE TABLE work_schema_version (
            version INTEGER NOT NULL,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO work_schema_version (version, description, applied_at)
        VALUES (7, 'pre-#1398', '2026-04-01T00:00:00+00:00');

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
            roles TEXT NOT NULL DEFAULT '{}',
            external_refs TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (project, task_number)
        );
        INSERT INTO work_tasks
            (project, task_number, title, type, flow_template_id,
             created_at, created_by, updated_at)
        VALUES
            ('demo', 1, 'pre-existing', 'task', 'standard',
             '2026-01-01T00:00:00+00:00', 'tester',
             '2026-01-01T00:00:00+00:00');
        """
    )

    create_work_tables(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(work_tasks)")}
    assert "plan_version" in cols, "plan_version column should exist post-migration"
    assert "predecessor_task_id" in cols, (
        "predecessor_task_id column should exist post-migration"
    )

    row = conn.execute(
        "SELECT title, plan_version, predecessor_task_id FROM work_tasks "
        "WHERE project = ? AND task_number = ?",
        ("demo", 1),
    ).fetchone()
    assert row is not None, "existing row must survive additive migration"
    assert row[0] == "pre-existing", "title must be untouched"
    assert row[1] == 1, "plan_version defaults to 1 for pre-#1398 rows"
    assert row[2] is None, "predecessor_task_id defaults to NULL"

    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM work_schema_version"
    ).fetchone()[0]
    assert version == 8

    idxs = _indexes(conn)
    assert "idx_work_tasks_predecessor" in idxs, (
        "successors lookup index should be created on legacy DB"
    )
