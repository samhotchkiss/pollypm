"""Tests for work service query operations: next, my_tasks, state_counts, blocked_tasks."""

from __future__ import annotations

import time

import pytest

from pollypm.work.models import Priority, WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def _create_task(svc, project="proj", title="Task", description="desc", priority="normal", roles=None, **kwargs):
    defaults = dict(
        title=title,
        description=description,
        type="task",
        project=project,
        flow_template="standard",
        roles=roles or {"worker": "agent-1", "reviewer": "agent-2"},
        priority=priority,
        created_by="tester",
    )
    defaults.update(kwargs)
    return svc.create(**defaults)


def _queue_task(svc, task):
    return svc.queue(task.task_id, "tester")


def _work_output_payload(summary="done"):
    return {
        "type": "code_change",
        "summary": summary,
        "artifacts": [{"kind": "note", "description": summary}],
    }


# ---------------------------------------------------------------------------
# next() tests
# ---------------------------------------------------------------------------


class TestNext:
    def test_next_returns_highest_priority(self, svc):
        t_high = _create_task(svc, title="High", priority="high")
        t_normal = _create_task(svc, title="Normal", priority="normal")
        t_critical = _create_task(svc, title="Critical", priority="critical")

        _queue_task(svc, t_high)
        _queue_task(svc, t_normal)
        _queue_task(svc, t_critical)

        result = svc.next()
        assert result is not None
        assert result.title == "Critical"

    def test_next_skips_blocked(self, svc):
        # Create a blocker task that stays in draft
        blocker = _create_task(svc, title="Blocker")

        # Task A: will be blocked
        task_a = _create_task(svc, title="Task A")
        _queue_task(svc, task_a)

        # Task B: not blocked
        task_b = _create_task(svc, title="Task B")
        _queue_task(svc, task_b)

        # Link blocker -> task_a (blocker blocks task_a)
        svc.link(blocker.task_id, task_a.task_id, "blocks")

        result = svc.next()
        assert result is not None
        assert result.title == "Task B"

    def test_next_filters_by_project(self, svc):
        t_alpha = _create_task(svc, project="alpha", title="Alpha task")
        t_beta = _create_task(svc, project="beta", title="Beta task")

        _queue_task(svc, t_alpha)
        _queue_task(svc, t_beta)

        result = svc.next(project="alpha")
        assert result is not None
        assert result.project == "alpha"
        assert result.title == "Alpha task"

    def test_next_filters_by_agent(self, svc):
        t_pete = _create_task(svc, title="Pete's task", roles={"worker": "pete", "reviewer": "polly"})
        t_sam = _create_task(svc, title="Sam's task", roles={"worker": "sam", "reviewer": "polly"})

        _queue_task(svc, t_pete)
        _queue_task(svc, t_sam)

        result = svc.next(agent="pete")
        assert result is not None
        assert result.title == "Pete's task"

    def test_next_fifo_within_priority(self, svc):
        # All normal priority; oldest should come first
        t1 = _create_task(svc, title="First")
        t2 = _create_task(svc, title="Second")
        t3 = _create_task(svc, title="Third")

        _queue_task(svc, t1)
        _queue_task(svc, t2)
        _queue_task(svc, t3)

        result = svc.next()
        assert result is not None
        assert result.title == "First"

    def test_next_returns_none_when_empty(self, svc):
        # No tasks at all
        assert svc.next() is None

        # Tasks exist but none are queued
        _create_task(svc, title="Draft task")
        assert svc.next() is None

    def test_next_does_not_call_per_task_blocker_lookup(self, svc, monkeypatch):
        blocker = _create_task(svc, title="Blocker")
        blocked = _create_task(svc, title="Blocked")
        winner = _create_task(svc, title="Winner")

        _queue_task(svc, blocked)
        _queue_task(svc, winner)
        svc.link(blocker.task_id, blocked.task_id, "blocks")

        def _fail(_: str) -> bool:
            raise AssertionError("next() should not call per-task blocker lookups")

        monkeypatch.setattr(svc, "_has_unresolved_blockers", _fail)

        result = svc.next()
        assert result is not None
        assert result.task_id == winner.task_id

    def test_next_hydrates_only_selected_matching_task(self, svc, monkeypatch):
        blocker = _create_task(svc, title="Blocker")
        blocked = _create_task(
            svc,
            title="Blocked",
            roles={"worker": "alice", "reviewer": "reviewer"},
        )
        wrong_agent = _create_task(
            svc,
            title="Wrong Agent",
            roles={"worker": "bob", "reviewer": "reviewer"},
        )
        winner = _create_task(
            svc,
            title="Winner",
            roles={"worker": "alice", "reviewer": "reviewer"},
        )

        _queue_task(svc, blocked)
        _queue_task(svc, wrong_agent)
        _queue_task(svc, winner)
        svc.link(blocker.task_id, blocked.task_id, "blocks")

        original = svc._row_to_task
        hydrate_calls = 0

        def _counting_row_to_task(*args, **kwargs):
            nonlocal hydrate_calls
            hydrate_calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(svc, "_row_to_task", _counting_row_to_task)

        result = svc.next(agent="alice")
        assert result is not None
        assert result.task_id == winner.task_id
        assert hydrate_calls == 1


# ---------------------------------------------------------------------------
# my_tasks() tests
# ---------------------------------------------------------------------------


class TestMyTasks:
    def test_my_tasks(self, svc):
        # Create two tasks with different workers, claim them
        t1 = _create_task(svc, title="Worker1 task", roles={"worker": "alice", "reviewer": "bob"})
        t2 = _create_task(svc, title="Worker2 task", roles={"worker": "bob", "reviewer": "alice"})

        _queue_task(svc, t1)
        _queue_task(svc, t2)

        # Claim both (sets current_node to 'implement' which has actor_role='worker')
        svc.claim(t1.task_id, "alice")
        svc.claim(t2.task_id, "bob")

        # alice's tasks: she is worker on t1 -> implement node -> actor_role=worker
        alice_tasks = svc.my_tasks("alice")
        assert len(alice_tasks) == 1
        assert alice_tasks[0].title == "Worker1 task"

        # bob's tasks: he is worker on t2
        bob_tasks = svc.my_tasks("bob")
        assert len(bob_tasks) == 1
        assert bob_tasks[0].title == "Worker2 task"

    def test_my_tasks_hands_off_to_reviewer_after_node_done(self, svc):
        task = _create_task(
            svc,
            title="Handoff task",
            roles={"worker": "alice", "reviewer": "bob"},
        )
        _queue_task(svc, task)
        svc.claim(task.task_id, "alice")

        svc.node_done(task.task_id, "alice", work_output=_work_output_payload("worker complete"))

        reloaded = svc.get(task.task_id)
        assert reloaded.current_node_id == "code_review"
        assert reloaded.assignee == "bob"
        assert [t.task_id for t in svc.my_tasks("alice")] == []
        assert [t.task_id for t in svc.my_tasks("bob")] == [task.task_id]

    def test_my_tasks_hands_back_to_worker_after_reject(self, svc):
        task = _create_task(
            svc,
            title="Reject loop task",
            roles={"worker": "alice", "reviewer": "bob"},
        )
        _queue_task(svc, task)
        svc.claim(task.task_id, "alice")
        svc.node_done(task.task_id, "alice", work_output=_work_output_payload("ready for review"))

        svc.reject(task.task_id, "bob", "needs changes")

        reloaded = svc.get(task.task_id)
        assert reloaded.current_node_id == "implement"
        assert reloaded.assignee == "alice"
        assert [t.task_id for t in svc.my_tasks("alice")] == [task.task_id]
        assert [t.task_id for t in svc.my_tasks("bob")] == []

    def test_my_tasks_filters_rows_before_hydration(self, svc, monkeypatch):
        tasks = [
            _create_task(
                svc,
                title="Alice task",
                roles={"worker": "alice", "reviewer": "reviewer-a"},
            ),
            _create_task(
                svc,
                title="Bob task",
                roles={"worker": "bob", "reviewer": "reviewer-b"},
            ),
            _create_task(
                svc,
                title="Carol task",
                roles={"worker": "carol", "reviewer": "reviewer-c"},
            ),
        ]
        for task, actor in zip(tasks, ("alice", "bob", "carol"), strict=True):
            _queue_task(svc, task)
            svc.claim(task.task_id, actor)

        hydrated: list[str] = []
        original_row_to_task = svc._row_to_task

        def counting_row_to_task(row, *args, **kwargs):
            hydrated.append(f"{row['project']}/{row['task_number']}")
            return original_row_to_task(row, *args, **kwargs)

        monkeypatch.setattr(svc, "_row_to_task", counting_row_to_task)
        monkeypatch.setattr(
            svc,
            "derive_owner",
            lambda task: (_ for _ in ()).throw(AssertionError("derive_owner should not run")),
        )

        alice_tasks = svc.my_tasks("alice")

        assert [task.task_id for task in alice_tasks] == [tasks[0].task_id]
        assert hydrated == [tasks[0].task_id]


# ---------------------------------------------------------------------------
# state_counts() tests
# ---------------------------------------------------------------------------


class TestStateCounts:
    def test_state_counts(self, svc):
        _create_task(svc, title="Draft 1")
        _create_task(svc, title="Draft 2")

        t3 = _create_task(svc, title="Queued 1")
        _queue_task(svc, t3)

        counts = svc.state_counts()
        assert counts["draft"] == 2
        assert counts["queued"] == 1
        assert counts["in_progress"] == 0
        assert counts["done"] == 0

    def test_state_counts_by_project(self, svc):
        _create_task(svc, project="alpha", title="Alpha draft")
        _create_task(svc, project="beta", title="Beta draft")

        t = _create_task(svc, project="alpha", title="Alpha queued")
        _queue_task(svc, t)

        counts = svc.state_counts(project="alpha")
        assert counts["draft"] == 1
        assert counts["queued"] == 1

        counts_beta = svc.state_counts(project="beta")
        assert counts_beta["draft"] == 1
        assert counts_beta["queued"] == 0


# ---------------------------------------------------------------------------
# blocked_tasks() tests
# ---------------------------------------------------------------------------


class TestBlockedTasks:
    def test_blocked_tasks(self, svc):
        blocker = _create_task(svc, title="Blocker")
        target = _create_task(svc, title="Target")
        unblocked = _create_task(svc, title="Unblocked")

        _queue_task(svc, target)
        _queue_task(svc, unblocked)

        # blocker blocks target
        svc.link(blocker.task_id, target.task_id, "blocks")

        blocked = svc.blocked_tasks()
        blocked_ids = [t.task_id for t in blocked]
        assert target.task_id in blocked_ids
        assert unblocked.task_id not in blocked_ids

    def test_blocked_tasks_by_project(self, svc):
        blocker = _create_task(svc, project="alpha", title="Blocker")
        target_alpha = _create_task(svc, project="alpha", title="Alpha target")
        target_beta = _create_task(svc, project="beta", title="Beta target")

        _queue_task(svc, target_alpha)
        _queue_task(svc, target_beta)

        # blocker blocks both targets
        svc.link(blocker.task_id, target_alpha.task_id, "blocks")
        svc.link(blocker.task_id, target_beta.task_id, "blocks")

        blocked_alpha = svc.blocked_tasks(project="alpha")
        blocked_ids = [t.task_id for t in blocked_alpha]
        assert target_alpha.task_id in blocked_ids
        assert target_beta.task_id not in blocked_ids

    def test_blocked_tasks_shows_cancelled_blocker_case(self, svc):
        """Per OQ-7: a task whose blocker was cancelled stays blocked until
        the PM decides. blocked_tasks() must surface it so the operator
        can act (#138)."""
        from pollypm.work.models import WorkStatus

        blocker = _create_task(svc, title="Will be cancelled")
        target = _create_task(svc, title="Stuck target")

        _queue_task(svc, target)
        svc.link(blocker.task_id, target.task_id, "blocks")

        # Task is now blocked
        assert svc.get(target.task_id).work_status == WorkStatus.BLOCKED

        # Cancel the blocker
        svc.cancel(blocker.task_id, "pm", "not needed after all")

        # Task must remain blocked (auto-unblock skipped — cancelled != done)
        stuck = svc.get(target.task_id)
        assert stuck.work_status == WorkStatus.BLOCKED

        # And it must show up in blocked_tasks() — this is the whole point
        # of the PM dashboard query.
        blocked = svc.blocked_tasks()
        assert target.task_id in [t.task_id for t in blocked]
