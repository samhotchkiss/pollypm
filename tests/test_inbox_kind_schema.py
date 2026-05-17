"""Schema + round-trip tests for the structured inbox ``kind`` taxonomy (#1565).

Covers:
* The :class:`InboxItemKind` enum values are stable (the value strings
  go onto disk; a typo would orphan every legacy row).
* The ``messages.kind`` column exists with default ``'legacy'`` and
  round-trips every enum value.
* The ``work_tasks.kind`` column exists with default ``'legacy'`` and
  round-trips through :meth:`WorkService.create`.
* Pre-migration rows (column absent in initial schema) gain
  ``kind='legacy'`` via the migration helper.
* ``pm inbox --json`` (well, ``_task_to_dict`` and ``_message_row_to_display``)
  surfaces the ``kind`` value on every item.
"""

from __future__ import annotations

import sqlite3

import pytest

from pollypm.inbox.kind import InboxItemKind, coerce_kind
from pollypm.store import SQLAlchemyStore
from pollypm.storage.state import StateStore
from pollypm.work.cli import _task_to_dict
from pollypm.work.inbox_cli import _message_row_to_display
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Enum stability
# ---------------------------------------------------------------------------


def test_inbox_item_kind_values_are_stable():
    """The on-disk strings must never drift. If you add a member, do it
    here; if you find yourself wanting to rename one, don't — every
    legacy row holds the literal string."""
    assert {k.value for k in InboxItemKind} == {
        "plan_review_pending",
        "approval_request",
        "pm_question_unanswered",
        "watchdog_operator_dispatch",
        "manual_decision",
        "completion_fyi",
        "self_bug_report",
        "activity_event",
        "info",
        "legacy",
    }


def test_coerce_kind_handles_unknown_and_none():
    assert coerce_kind(None) is InboxItemKind.LEGACY
    assert coerce_kind("not-a-real-kind") is InboxItemKind.LEGACY
    assert coerce_kind("plan_review_pending") is InboxItemKind.PLAN_REVIEW_PENDING
    assert coerce_kind(InboxItemKind.MANUAL_DECISION) is InboxItemKind.MANUAL_DECISION


# ---------------------------------------------------------------------------
# work_tasks.kind — schema + round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def test_work_tasks_kind_column_shape(svc):
    """``work_tasks.kind`` exists, is TEXT NOT NULL, defaults to 'legacy'."""
    cols = {
        row[1]: {
            "type": row[2],
            "notnull": bool(row[3]),
            "default": row[4],
        }
        for row in svc._conn.execute("PRAGMA table_info(work_tasks)")
    }
    assert "kind" in cols, "work_tasks.kind column is missing"
    info = cols["kind"]
    assert info["type"] == "TEXT"
    assert info["notnull"] is True
    # SQLite quotes string defaults; accept either form.
    assert info["default"] in ("'legacy'", "legacy")


@pytest.mark.parametrize("kind", list(InboxItemKind))
def test_work_tasks_kind_round_trip_via_create(svc, kind):
    """Every enum value survives create() -> get() unchanged."""
    task = svc.create(
        title=f"task for {kind.value}",
        description="round-trip",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        created_by="tester",
        kind=kind.value,
    )
    loaded = svc.get(task.task_id)
    assert loaded.kind is kind


def test_work_tasks_kind_default_for_pre_migration_row(tmp_path):
    """A legacy DB whose ``work_tasks`` table predates the column gets
    ``kind='legacy'`` on every existing row after migration."""
    db_path = tmp_path / "legacy_work.db"
    # Build a pre-#1565 work_tasks table by hand, insert a row, then
    # let SQLiteWorkService run its migrations.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
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
        INSERT INTO work_tasks (
            project, task_number, title, type, flow_template_id,
            created_at, created_by, updated_at
        ) VALUES (
            'proj', 1, 'pre-migration task', 'task', 'standard',
            '2026-04-01T00:00:00+00:00', 'tester', '2026-04-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    svc = SQLiteWorkService(db_path=db_path)
    cols = {row[1] for row in svc._conn.execute("PRAGMA table_info(work_tasks)")}
    assert "kind" in cols
    row = svc._conn.execute(
        "SELECT kind FROM work_tasks WHERE project = ? AND task_number = ?",
        ("proj", 1),
    ).fetchone()
    assert row["kind"] == "legacy"


# ---------------------------------------------------------------------------
# messages.kind — schema + round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "messages.db"
    return SQLAlchemyStore(f"sqlite:///{db_path}")


def test_messages_kind_column_shape(store):
    """``messages.kind`` exists, NOT NULL, default 'legacy'."""
    with store.read_engine.connect() as conn:
        cols = {
            row[1]: {
                "type": row[2],
                "notnull": bool(row[3]),
                "default": row[4],
            }
            for row in conn.exec_driver_sql("PRAGMA table_info(messages)")
        }
    assert "kind" in cols
    info = cols["kind"]
    # SQLAlchemy maps Python ``String`` to ``VARCHAR`` on SQLite.
    assert info["type"] in ("TEXT", "VARCHAR")
    assert info["notnull"] is True
    assert info["default"] in ("'legacy'", "legacy")


@pytest.mark.parametrize("kind", list(InboxItemKind))
def test_messages_kind_round_trip_via_enqueue(store, kind):
    """``enqueue_message`` writes the stamped kind; query_messages returns it."""
    msg_id = store.enqueue_message(
        type="notify",
        tier="immediate",
        recipient="user",
        sender="polly",
        subject=f"round-trip {kind.value}",
        body="",
        scope="root",
        kind=kind.value,
    )
    rows = store.query_messages(recipient="user")
    match = next(row for row in rows if row["id"] == msg_id)
    assert match["kind"] == kind.value


def test_messages_kind_default_for_pre_migration_row(tmp_path):
    """A legacy ``messages`` table without ``kind`` gets ``'legacy'``
    on every existing row after StateStore migration v18."""
    db_path = tmp_path / "legacy_state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_version (
            version INTEGER NOT NULL,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT
        );
        INSERT INTO schema_version (version, description, applied_at)
        VALUES (17, 'pre-#1565', '2026-04-01T00:00:00Z');
        INSERT INTO messages (scope, type, recipient, sender, subject)
        VALUES ('root', 'notify', 'user', 'polly', '[Action] pre-migration row');
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)
    try:
        cols = {
            row[1] for row in store.execute("PRAGMA table_info(messages)")
        }
        assert "kind" in cols
        row = store.execute(
            "SELECT kind FROM messages WHERE subject = ?",
            ("[Action] pre-migration row",),
        ).fetchone()
        assert row[0] == "legacy"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# pm inbox --json surface
# ---------------------------------------------------------------------------


def test_pm_inbox_json_includes_kind_for_message_rows(store):
    """``_message_row_to_display`` (the JSON shape) surfaces ``kind``."""
    msg_id = store.enqueue_message(
        type="notify",
        tier="immediate",
        recipient="user",
        sender="polly",
        subject="hello",
        body="",
        scope="root",
        kind=InboxItemKind.APPROVAL_REQUEST.value,
    )
    rows = store.query_messages(recipient="user")
    match = next(row for row in rows if row["id"] == msg_id)
    display = _message_row_to_display(match)
    assert display["kind"] == "approval_request"


def test_pm_inbox_json_includes_kind_for_task_rows(svc):
    """``_task_to_dict`` (the JSON shape) surfaces ``kind``."""
    task = svc.create(
        title="needs you",
        description="please decide",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        created_by="tester",
        kind=InboxItemKind.MANUAL_DECISION.value,
    )
    payload = _task_to_dict(svc.get(task.task_id))
    assert payload["kind"] == "manual_decision"
