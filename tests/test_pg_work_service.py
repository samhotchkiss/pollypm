"""Tests for the Slice A subset of :class:`PgWorkService` (issue #1737).

Covers the read + minimal-CRUD surface shipped in Slice A:

* ``__init__`` opens a pool and runs the schema migration applier.
* ``create`` allocates a new task_number atomically per project.
* ``get`` round-trips every column in the schema port.
* ``list_tasks`` honours the supported filter subset.
* ``queue`` / ``mark_done`` / ``cancel`` write through the
  ``work_transitions`` audit trail.
* ``state_counts`` aggregates by status.

The ``pg_schema_pool`` fixture comes from ``tests/conftest_pg.py``; if
Docker / local pg isn't available the whole module skips.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def pg_service(pg_schema_pool):
    from pollypm.work.pg_service import PgWorkService

    return PgWorkService(pool=pg_schema_pool, ro_pool=None)


def test_init_applies_schema(pg_schema_pool):
    """Constructor must apply the schema migration on first open."""
    from pollypm.work.pg_service import PgWorkService

    PgWorkService(pool=pg_schema_pool, ro_pool=None)
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_tables "
            "WHERE schemaname = current_schema() "
            "AND tablename = 'work_tasks'"
        )
        assert cur.fetchone() is not None


def test_create_assigns_sequential_task_numbers(pg_service):
    t1 = pg_service.create(
        title="first",
        type="task",
        project="demo",
        flow_template="default",
        roles={"worker": "alice"},
    )
    t2 = pg_service.create(
        title="second",
        type="task",
        project="demo",
        flow_template="default",
        roles={"worker": "alice"},
    )
    assert t1.task_number == 1
    assert t2.task_number == 2
    assert t1.project == "demo"


def test_create_per_project_isolation(pg_service):
    """Task numbers must increment per project, not globally."""
    a = pg_service.create(
        title="a",
        type="task",
        project="proj-a",
        flow_template="default",
        roles={"worker": "alice"},
    )
    b = pg_service.create(
        title="b",
        type="task",
        project="proj-b",
        flow_template="default",
        roles={"worker": "alice"},
    )
    assert a.task_number == 1
    assert b.task_number == 1


def test_get_round_trips_fields(pg_service):
    from pollypm.inbox.kind import InboxItemKind
    from pollypm.work.models import Priority, TaskType, WorkStatus

    created = pg_service.create(
        title="round trip",
        description="full payload",
        type="bug",
        project="demo",
        flow_template="custom",
        roles={"worker": "alice", "reviewer": "bob"},
        priority="high",
        labels=["urgent", "backend"],
        relevant_files=["a.py", "b.py"],
        kind="approval_request",
    )

    task = pg_service.get(f"demo/{created.task_number}")
    assert task.title == "round trip"
    assert task.description == "full payload"
    assert task.type is TaskType.BUG
    assert task.priority is Priority.HIGH
    assert task.work_status is WorkStatus.DRAFT
    assert task.labels == ["urgent", "backend"]
    assert task.relevant_files == ["a.py", "b.py"]
    assert task.roles == {"worker": "alice", "reviewer": "bob"}
    assert task.kind is InboxItemKind.APPROVAL_REQUEST


def test_get_missing_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.get("nope/999")


def test_list_tasks_filters_by_project(pg_service):
    pg_service.create(
        title="a", type="task", project="alpha",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.create(
        title="b", type="task", project="beta",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.create(
        title="c", type="task", project="alpha",
        flow_template="default", roles={"worker": "a"},
    )
    alpha = pg_service.list_tasks(project="alpha")
    beta = pg_service.list_tasks(project="beta")
    assert {t.title for t in alpha} == {"a", "c"}
    assert {t.title for t in beta} == {"b"}


def test_list_tasks_filter_by_status(pg_service):
    a = pg_service.create(
        title="a", type="task", project="demo",
        flow_template="default", roles={"worker": "x"},
    )
    pg_service.create(
        title="b", type="task", project="demo",
        flow_template="default", roles={"worker": "x"},
    )
    pg_service.queue(f"demo/{a.task_number}", actor="user")
    queued = pg_service.list_tasks(project="demo", work_status="queued")
    assert {t.title for t in queued} == {"a"}


def test_queue_transitions_draft_to_queued(pg_service):
    from pollypm.work.models import WorkStatus

    task = pg_service.create(
        title="x", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    queued = pg_service.queue(f"demo/{task.task_number}", actor="user")
    assert queued.work_status is WorkStatus.QUEUED


def test_queue_writes_transition_row(pg_service, pg_schema_pool):
    task = pg_service.create(
        title="x", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.queue(f"demo/{task.task_number}", actor="user")
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT from_state, to_state, actor FROM work_transitions "
            "WHERE task_project = 'demo' AND task_number = %s",
            (task.task_number,),
        )
        rows = cur.fetchall()
    assert ("draft", "queued", "user") in [(r[0], r[1], r[2]) for r in rows]


def test_queue_is_idempotent(pg_service):
    from pollypm.work.models import WorkStatus

    task = pg_service.create(
        title="x", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.queue(f"demo/{task.task_number}", actor="user")
    second = pg_service.queue(f"demo/{task.task_number}", actor="user")
    assert second.work_status is WorkStatus.QUEUED


def test_cancel_transitions_to_cancelled(pg_service):
    from pollypm.work.models import WorkStatus

    task = pg_service.create(
        title="x", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    cancelled = pg_service.cancel(
        f"demo/{task.task_number}", actor="user", reason="moot"
    )
    assert cancelled.work_status is WorkStatus.CANCELLED


def test_cancel_rejects_terminal_task(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = pg_service.create(
        title="x", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.mark_done(f"demo/{task.task_number}", actor="user")
    with pytest.raises(InvalidTransitionError):
        pg_service.cancel(
            f"demo/{task.task_number}", actor="user", reason="nope"
        )


def test_mark_done_transitions_to_done(pg_service):
    from pollypm.work.models import WorkStatus

    task = pg_service.create(
        title="x", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    done = pg_service.mark_done(f"demo/{task.task_number}", actor="user")
    assert done.work_status is WorkStatus.DONE


def test_state_counts_aggregates(pg_service):
    a = pg_service.create(
        title="a", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.create(
        title="b", type="task", project="demo",
        flow_template="default", roles={"worker": "a"},
    )
    pg_service.queue(f"demo/{a.task_number}", actor="user")
    counts = pg_service.state_counts(project="demo")
    assert counts.get("draft", 0) == 1
    assert counts.get("queued", 0) == 1


def test_list_nonterminal_excludes_done_and_cancelled(pg_service):
    a = pg_service.create(
        title="a", type="task", project="demo",
        flow_template="default", roles={"worker": "x"},
    )
    b = pg_service.create(
        title="b", type="task", project="demo",
        flow_template="default", roles={"worker": "x"},
    )
    c = pg_service.create(
        title="c", type="task", project="demo",
        flow_template="default", roles={"worker": "x"},
    )
    pg_service.mark_done(f"demo/{a.task_number}", actor="u")
    pg_service.cancel(f"demo/{b.task_number}", actor="u", reason="r")
    rows = pg_service.list_nonterminal_tasks(project="demo")
    assert [t.task_number for t in rows] == [c.task_number]


def test_slice_b_methods_are_implemented(pg_service):
    """Slice B (#1737) ships claim / approve / etc. — no NotImplementedError."""
    from pollypm.work.service_support import TaskNotFoundError

    # Methods now reach the DB and surface domain errors instead of
    # NotImplementedError. The missing-task lookup is the cheapest way
    # to prove "code ran past the stub".
    with pytest.raises(TaskNotFoundError):
        pg_service.claim("demo/999", actor="u")
    with pytest.raises(TaskNotFoundError):
        pg_service.approve("demo/999", actor="u")
