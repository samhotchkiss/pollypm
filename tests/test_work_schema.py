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


class TestIndexes:
    def test_key_indexes_exist(self, conn):
        create_work_tables(conn)
        idxs = _indexes(conn)
        expected = {
            "idx_work_tasks_status",
            "idx_work_tasks_project_status",
            "idx_work_tasks_assignee",
            "idx_work_tasks_priority",
            "idx_work_deps_to",
            "idx_work_exec_task",
            "idx_work_context_task",
            "idx_work_transitions_task",
        }
        for name in expected:
            assert name in idxs, f"Missing index: {name}"
