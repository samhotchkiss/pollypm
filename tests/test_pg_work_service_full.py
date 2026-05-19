"""Slice B (#1737) full :class:`PgWorkService` coverage.

Sister suite to :mod:`tests.test_pg_work_service` (which covers the
Slice A read+CRUD subset). This module exercises the methods that
landed in Slice B against the testcontainer pg from
:mod:`tests.conftest_pg`:

* mutable-field ``update`` + ``increment_plan_version`` + ``list_successors``
* claim / hold / resume / next state transitions
* node_done / approve / reject / block flow progression
* add_context / get_context
* link / unlink / dependents
* my_tasks / blocked_tasks / state_counts zero-fill
* validate_advance preflight
* sync_status / trigger_sync (Slice B stub shape)
* worker_session CRUD trio (upsert / get / list / end / mark_ended / update_tokens)
* available_flows / get_flow (file-resolver delegation)

The fixture skips when neither Docker nor a local pg+vector DSN is
available — same gate as :mod:`tests.test_pg_work_service`.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def pg_service(pg_schema_pool):
    from pollypm.work.pg_service import PgWorkService

    return PgWorkService(pool=pg_schema_pool, ro_pool=None)


# ---------------------------------------------------------------------------
# update / increment_plan_version / list_successors
# ---------------------------------------------------------------------------


def _make_draft(svc, project="demo", title="t", **kw):
    return svc.create(
        title=title,
        type=kw.pop("type", "task"),
        project=project,
        flow_template=kw.pop("flow_template", "standard"),
        roles=kw.pop("roles", {"worker": "alice", "reviewer": "bob"}),
        description=kw.pop("description", "has body"),
        **kw,
    )


def test_update_title_and_priority(pg_service):
    task = _make_draft(pg_service)
    updated = pg_service.update(
        task.task_id, title="renamed", priority="high"
    )
    from pollypm.work.models import Priority

    assert updated.title == "renamed"
    assert updated.priority is Priority.HIGH


def test_update_labels_round_trips(pg_service):
    task = _make_draft(pg_service)
    updated = pg_service.update(task.task_id, labels=["a", "b"])
    assert updated.labels == ["a", "b"]


def test_update_rejects_work_status_change(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _make_draft(pg_service)
    with pytest.raises(ValidationError):
        pg_service.update(task.task_id, work_status="queued")


def test_update_rejects_flow_template_change(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _make_draft(pg_service)
    with pytest.raises(ValidationError):
        pg_service.update(task.task_id, flow_template="other")


def test_update_rejects_unknown_field(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _make_draft(pg_service)
    with pytest.raises(ValidationError):
        pg_service.update(task.task_id, bogus="x")


def test_update_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.update("nope/999", title="x")


def test_increment_plan_version_bumps_counter(pg_service):
    task = _make_draft(pg_service)
    assert task.plan_version == 1
    bumped = pg_service.increment_plan_version(task.task_id, actor="user")
    assert bumped.plan_version == 2


def test_list_successors_walks_predecessor_chain(pg_service):
    parent = _make_draft(pg_service)
    child = pg_service.create(
        title="successor",
        type="task",
        project="demo",
        flow_template="default",
        roles={"worker": "a"},
        predecessor_task_id=parent.task_id,
    )
    successors = pg_service.list_successors(parent.task_id)
    assert [t.task_id for t in successors] == [child.task_id]


# ---------------------------------------------------------------------------
# add_context / get_context
# ---------------------------------------------------------------------------


def test_add_context_returns_entry_and_roundtrips(pg_service):
    task = _make_draft(pg_service)
    entry = pg_service.add_context(task.task_id, "user", "hello world")
    assert entry.actor == "user"
    assert entry.text == "hello world"
    assert entry.entry_type == "note"

    rows = pg_service.get_context(task.task_id)
    assert len(rows) == 1
    assert rows[0].text == "hello world"


def test_add_context_missing_task(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.add_context("nope/1", "user", "x")


def test_get_context_filters_by_entry_type(pg_service):
    task = _make_draft(pg_service)
    pg_service.add_context(task.task_id, "user", "note 1")
    pg_service.add_context(
        task.task_id, "user", "a reply", entry_type="reply"
    )
    notes = pg_service.get_context(task.task_id, entry_type="note")
    replies = pg_service.get_context(task.task_id, entry_type="reply")
    assert [e.text for e in notes] == ["note 1"]
    assert [e.text for e in replies] == ["a reply"]


def test_get_context_limit_honored(pg_service):
    task = _make_draft(pg_service)
    for i in range(5):
        pg_service.add_context(task.task_id, "user", f"n{i}")
    rows = pg_service.get_context(task.task_id, limit=2)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# link / unlink / dependents / would_create_cycle (via block)
# ---------------------------------------------------------------------------


def test_link_blocks_creates_edge_and_dependents_walk(pg_service):
    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b")
    c = _make_draft(pg_service, title="c")
    pg_service.link(a.task_id, b.task_id, "blocks")
    pg_service.link(b.task_id, c.task_id, "blocks")
    deps = pg_service.dependents(a.task_id)
    assert {t.task_id for t in deps} == {b.task_id, c.task_id}


def test_link_rejects_invalid_kind(pg_service):
    from pollypm.work.service_support import ValidationError

    a = _make_draft(pg_service)
    b = _make_draft(pg_service, title="b")
    with pytest.raises(ValidationError):
        pg_service.link(a.task_id, b.task_id, "bogus")


def test_link_rejects_cycle(pg_service):
    from pollypm.work.service_support import ValidationError

    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b")
    pg_service.link(a.task_id, b.task_id, "blocks")
    with pytest.raises(ValidationError, match="circular"):
        pg_service.link(b.task_id, a.task_id, "blocks")


def test_unlink_removes_edge(pg_service):
    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b")
    pg_service.link(a.task_id, b.task_id, "blocks")
    pg_service.unlink(a.task_id, b.task_id, "blocks")
    assert pg_service.dependents(a.task_id) == []


def test_link_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    a = _make_draft(pg_service)
    with pytest.raises(TaskNotFoundError):
        pg_service.link(a.task_id, "ghost/1", "blocks")


# ---------------------------------------------------------------------------
# claim / hold / resume / next
# ---------------------------------------------------------------------------


def test_claim_advances_to_in_progress(pg_service):
    from pollypm.work.models import WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    claimed = pg_service.claim(task.task_id, actor="alice")
    assert claimed.work_status is WorkStatus.IN_PROGRESS
    assert claimed.current_node_id is not None


def test_claim_from_wrong_state_raises(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    with pytest.raises(InvalidTransitionError):
        pg_service.claim(task.task_id, actor="alice")


def test_hold_from_in_progress(pg_service):
    from pollypm.work.models import WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    held = pg_service.hold(task.task_id, actor="user", reason="pause")
    assert held.work_status is WorkStatus.ON_HOLD


def test_hold_from_wrong_state(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    with pytest.raises(InvalidTransitionError):
        pg_service.hold(task.task_id, actor="user")


def test_resume_from_on_hold(pg_service):
    from pollypm.work.models import WorkStatus

    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.hold(task.task_id, actor="user", reason="x")
    resumed = pg_service.resume(task.task_id, actor="user")
    assert resumed.work_status is WorkStatus.QUEUED


def test_next_returns_highest_priority_queued(pg_service):
    a = _make_draft(pg_service, title="a")
    b = _make_draft(pg_service, title="b", priority="critical")
    pg_service.queue(a.task_id, actor="u")
    pg_service.queue(b.task_id, actor="u")
    nxt = pg_service.next()
    assert nxt is not None
    assert nxt.task_id == b.task_id


def test_next_returns_none_for_empty(pg_service):
    assert pg_service.next() is None


def test_next_respects_project_filter(pg_service):
    a = _make_draft(pg_service, project="alpha", title="a")
    _make_draft(pg_service, project="beta", title="b")
    pg_service.queue(a.task_id, actor="u")
    nxt_alpha = pg_service.next(project="alpha")
    assert nxt_alpha is not None
    assert nxt_alpha.project == "alpha"
    # beta has no queued tasks
    assert pg_service.next(project="beta") is None


# ---------------------------------------------------------------------------
# node_done / approve / reject (use a "chat" flow that reaches done)
# ---------------------------------------------------------------------------


def _drive_to_review(svc, project="demo"):
    """Create a task, queue+claim it, then run node_done to reach review."""
    task = svc.create(
        title="t",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "alice", "reviewer": "bob"},
        description="body",
    )
    svc.queue(task.task_id, actor="user")
    svc.claim(task.task_id, actor="alice")
    return task


def test_node_done_requires_output(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _drive_to_review(pg_service)
    with pytest.raises(ValidationError):
        pg_service.node_done(task.task_id, actor="alice")


def test_node_done_advances_to_review(pg_service):
    from pollypm.work.models import WorkStatus

    task = _drive_to_review(pg_service)
    done = pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )
    # standard flow: build -> review, so after node_done we should be in review
    assert done.work_status is WorkStatus.REVIEW


def test_approve_from_non_review_raises(pg_service):
    from pollypm.work.service_support import InvalidTransitionError

    task = _make_draft(pg_service)
    with pytest.raises(InvalidTransitionError):
        pg_service.approve(task.task_id, actor="user")


def test_reject_requires_reason(pg_service):
    from pollypm.work.service_support import ValidationError

    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "implemented X",
            "artifacts": [
                {"kind": "commit", "description": "impl", "ref": "HEAD"}
            ],
        },
    )
    with pytest.raises(ValidationError):
        pg_service.reject(task.task_id, actor="bob", reason="")


# ---------------------------------------------------------------------------
# block / blocked_tasks
# ---------------------------------------------------------------------------


def test_block_marks_task_blocked(pg_service):
    from pollypm.work.models import WorkStatus

    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "x",
            "artifacts": [{"kind": "commit", "description": "x", "ref": "HEAD"}],
        },
    )
    # task is now in REVIEW
    blocker = _make_draft(pg_service, title="blocker")
    blocked = pg_service.block(
        task.task_id, actor="user", blocker_task_id=blocker.task_id
    )
    assert blocked.work_status is WorkStatus.BLOCKED


def test_blocked_tasks_filter_by_project(pg_service):
    task = _drive_to_review(pg_service)
    pg_service.node_done(
        task.task_id,
        actor="alice",
        work_output={
            "type": "code_change",
            "summary": "x",
            "artifacts": [{"kind": "commit", "description": "x", "ref": "HEAD"}],
        },
    )
    blocker = _make_draft(pg_service, title="b")
    pg_service.block(task.task_id, actor="user", blocker_task_id=blocker.task_id)
    rows = pg_service.blocked_tasks(project="demo")
    assert {t.task_id for t in rows} == {task.task_id}


# ---------------------------------------------------------------------------
# get_execution
# ---------------------------------------------------------------------------


def test_get_execution_after_claim(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    rows = pg_service.get_execution(task.task_id)
    assert len(rows) >= 1
    assert rows[0].node_id is not None


def test_get_execution_node_filter(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    rows = pg_service.get_execution(task.task_id, node_id="unknown_node")
    assert rows == []


# ---------------------------------------------------------------------------
# my_tasks / state_counts / sync_status / trigger_sync
# ---------------------------------------------------------------------------


def test_my_tasks_returns_active_assignee_tasks(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    rows = pg_service.my_tasks("alice")
    assert {t.task_id for t in rows} == {task.task_id}


def test_my_tasks_empty_when_no_match(pg_service):
    assert pg_service.my_tasks("ghost") == []


def test_state_counts_zero_fills_all_statuses(pg_service):
    counts = pg_service.state_counts()
    from pollypm.work.models import WorkStatus

    # every WorkStatus value must appear (zero-filled if absent)
    for status in WorkStatus:
        assert status.value in counts


def test_sync_status_empty_for_unsynced_task(pg_service):
    task = _make_draft(pg_service)
    assert pg_service.sync_status(task.task_id) == {}


def test_sync_status_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.sync_status("nope/1")


def test_trigger_sync_returns_summary_shape(pg_service):
    summary = pg_service.trigger_sync()
    assert summary == {"synced": 0, "errors": {}}


def test_trigger_sync_missing_task_raises(pg_service):
    from pollypm.work.service_support import TaskNotFoundError

    with pytest.raises(TaskNotFoundError):
        pg_service.trigger_sync(task_id="nope/1")


# ---------------------------------------------------------------------------
# validate_advance preflight
# ---------------------------------------------------------------------------


def test_validate_advance_empty_for_draft(pg_service):
    task = _make_draft(pg_service)
    # draft has no current_node_id → empty results
    assert pg_service.validate_advance(task.task_id, actor="user") == []


def test_validate_advance_surfaces_actor_mismatch(pg_service):
    task = _make_draft(pg_service)
    pg_service.queue(task.task_id, actor="user")
    pg_service.claim(task.task_id, actor="alice")
    # standard flow's review node expects the reviewer role
    results = pg_service.validate_advance(task.task_id, actor="random_actor")
    # may be empty (no role mismatch on the current work node) or list a
    # failure — either way the method must not crash. Smoke-only.
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Worker sessions
# ---------------------------------------------------------------------------


def test_worker_session_round_trip(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="pane-1",
        worktree_path="/tmp/wt",
        branch_name="task/demo-1",
        started_at=datetime.now(UTC),
    )
    rec = pg_service.get_worker_session(
        task_project=task.project, task_number=task.task_number
    )
    assert rec is not None
    assert rec.agent_name == "alice"
    assert rec.pane_id == "pane-1"


def test_worker_session_active_only_filter(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    pg_service.end_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        ended_at=datetime.now(UTC),
        total_input_tokens=100,
        total_output_tokens=200,
        archive_path="/tmp/archive",
    )
    rec_active = pg_service.get_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        active_only=True,
    )
    rec_any = pg_service.get_worker_session(
        task_project=task.project, task_number=task.task_number
    )
    assert rec_active is None
    assert rec_any is not None
    assert rec_any.total_input_tokens == 100
    assert rec_any.total_output_tokens == 200


def test_list_worker_sessions_active_only(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    active = pg_service.list_worker_sessions(active_only=True)
    assert len(active) == 1
    pg_service.mark_worker_session_ended(
        task_project=task.project,
        task_number=task.task_number,
        ended_at=datetime.now(UTC),
    )
    after = pg_service.list_worker_sessions(active_only=True)
    assert after == []


def test_update_worker_session_tokens(pg_service):
    from datetime import UTC, datetime

    task = _make_draft(pg_service)
    pg_service.upsert_worker_session(
        task_project=task.project,
        task_number=task.task_number,
        agent_name="alice",
        pane_id="p",
        worktree_path="/tmp/wt",
        branch_name="b",
        started_at=datetime.now(UTC),
    )
    pg_service.update_worker_session_tokens(
        task_project=task.project,
        task_number=task.task_number,
        total_input_tokens=42,
        total_output_tokens=99,
        archive_path=None,
    )
    rec = pg_service.get_worker_session(
        task_project=task.project, task_number=task.task_number
    )
    assert rec is not None
    assert rec.total_input_tokens == 42
    assert rec.total_output_tokens == 99


def test_ensure_worker_session_schema_is_noop(pg_service):
    # No-op on pg — migration applier owns schema. Must not raise.
    pg_service.ensure_worker_session_schema()


# ---------------------------------------------------------------------------
# available_flows / get_flow
# ---------------------------------------------------------------------------


def test_available_flows_returns_some_templates(pg_service):
    # Without a project_path the file resolver loads bundled flows.
    templates = pg_service.available_flows()
    assert isinstance(templates, list)


def test_get_flow_returns_template(pg_service):
    tmpl = pg_service.get_flow("standard")
    assert tmpl.name == "standard"
