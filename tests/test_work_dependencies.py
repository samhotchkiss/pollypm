"""Tests for work service dependencies, blocking, and context log."""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

import pytest

from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import (
    SQLiteWorkService,
    TaskNotFoundError,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    db_path = tmp_path / "work.db"
    return SQLiteWorkService(db_path=db_path)


def _mk(svc, project="proj", title="Task", description="Do it", **kw):
    """Create a task with standard flow and roles."""
    defaults = dict(
        title=title,
        description=description,
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )
    defaults.update(kw)
    return svc.create(**defaults)


def _queue(svc, task_id, actor="pm"):
    return svc.queue(task_id, actor)


# ---------------------------------------------------------------------------
# Dependencies: link / unlink
# ---------------------------------------------------------------------------


class TestLinkBlocks:
    def test_link_blocks(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        svc.link(a.task_id, b.task_id, "blocks")

        a2 = svc.get(a.task_id)
        b2 = svc.get(b.task_id)
        assert (b2.project, b2.task_number) in a2.blocks
        assert (a2.project, a2.task_number) in b2.blocked_by

    def test_link_validates_both_exist(self, svc):
        a = _mk(svc, title="A")
        with pytest.raises(TaskNotFoundError):
            svc.link(a.task_id, "proj/999", "blocks")

    def test_link_relates_to(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        svc.link(a.task_id, b.task_id, "relates_to")

        a2 = svc.get(a.task_id)
        b2 = svc.get(b.task_id)
        assert (b.project, b.task_number) in a2.relates_to
        assert (a.project, a.task_number) in b2.relates_to

    def test_link_parent(self, svc):
        epic = _mk(svc, title="Epic", type="epic")
        task = _mk(svc, title="Task")
        svc.link(epic.task_id, task.task_id, "parent")

        epic2 = svc.get(epic.task_id)
        task2 = svc.get(task.task_id)
        assert (task.project, task.task_number) in epic2.children
        # task2 has epic as parent via incoming parent edge — we don't override
        # column-based parent fields from the link table currently, but the
        # relationship is recorded.


class TestCircularDependency:
    def test_circular_dependency_rejected(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        svc.link(a.task_id, b.task_id, "blocks")

        with pytest.raises(ValidationError, match="circular dependency detected"):
            svc.link(b.task_id, a.task_id, "blocks")

    def test_transitive_circular_rejected(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        c = _mk(svc, title="C")
        svc.link(a.task_id, b.task_id, "blocks")
        svc.link(b.task_id, c.task_id, "blocks")

        with pytest.raises(ValidationError, match="circular dependency detected"):
            svc.link(c.task_id, a.task_id, "blocks")


class TestUnlink:
    def test_unlink(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        svc.link(a.task_id, b.task_id, "blocks")

        # Verify link exists
        a2 = svc.get(a.task_id)
        assert len(a2.blocks) == 1

        svc.unlink(a.task_id, b.task_id, "blocks")

        a3 = svc.get(a.task_id)
        b3 = svc.get(b.task_id)
        assert len(a3.blocks) == 0
        assert len(b3.blocked_by) == 0


class TestDependents:
    def test_dependents_direct(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        c = _mk(svc, title="C")
        svc.link(a.task_id, b.task_id, "blocks")
        svc.link(a.task_id, c.task_id, "blocks")

        deps = svc.dependents(a.task_id)
        dep_ids = {d.task_id for d in deps}
        assert b.task_id in dep_ids
        assert c.task_id in dep_ids

    def test_dependents_transitive(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        c = _mk(svc, title="C")
        svc.link(a.task_id, b.task_id, "blocks")
        svc.link(b.task_id, c.task_id, "blocks")

        deps = svc.dependents(a.task_id)
        dep_ids = {d.task_id for d in deps}
        assert b.task_id in dep_ids
        assert c.task_id in dep_ids


# ---------------------------------------------------------------------------
# Blocked derivation
# ---------------------------------------------------------------------------


class TestBlockedDerivation:
    def test_blocked_derivation(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")

        # Queue both, then link A blocks B
        _queue(svc, a.task_id)
        _queue(svc, b.task_id)

        # Claim A so it is in_progress
        svc.claim(a.task_id, "agent-1")

        svc.link(a.task_id, b.task_id, "blocks")

        b2 = svc.get(b.task_id)
        assert b2.blocked is True
        assert b2.work_status == WorkStatus.BLOCKED

        # Move A to done
        svc.mark_done(a.task_id, "agent-1")

        b3 = svc.get(b.task_id)
        assert b3.blocked is False

    def test_blocked_by_cancelled_stays_blocked(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")

        _queue(svc, a.task_id)
        _queue(svc, b.task_id)

        svc.link(a.task_id, b.task_id, "blocks")

        b2 = svc.get(b.task_id)
        assert b2.work_status == WorkStatus.BLOCKED

        # Cancel A
        svc.cancel(a.task_id, "pm", "no longer needed")

        b3 = svc.get(b.task_id)
        assert b3.blocked is True  # still blocked because cancelled != done

        # Context log entry should exist about cancelled blocker
        ctx = svc.get_context(b.task_id)
        assert len(ctx) >= 1
        assert "cancelled" in ctx[0].text


class TestAutoUnblock:
    def test_auto_unblock_on_done(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")

        _queue(svc, a.task_id)
        _queue(svc, b.task_id)

        svc.link(a.task_id, b.task_id, "blocks")

        b2 = svc.get(b.task_id)
        assert b2.work_status == WorkStatus.BLOCKED

        # Move A to done
        svc.mark_done(a.task_id, "agent-1")

        b3 = svc.get(b.task_id)
        assert b3.work_status == WorkStatus.QUEUED

    def test_auto_unblock_multiple_blockers(self, svc):
        a = _mk(svc, title="A")
        b = _mk(svc, title="B")
        c = _mk(svc, title="C")

        _queue(svc, a.task_id)
        _queue(svc, b.task_id)
        _queue(svc, c.task_id)

        svc.link(a.task_id, b.task_id, "blocks")
        svc.link(c.task_id, b.task_id, "blocks")

        b2 = svc.get(b.task_id)
        assert b2.work_status == WorkStatus.BLOCKED

        # Move A to done — B still blocked by C
        svc.mark_done(a.task_id, "agent-1")
        b3 = svc.get(b.task_id)
        assert b3.work_status == WorkStatus.BLOCKED

        # Move C to done — B auto-unblocks
        svc.mark_done(c.task_id, "agent-1")
        b4 = svc.get(b.task_id)
        assert b4.work_status == WorkStatus.QUEUED


class TestCrossProjectLink:
    def test_cross_project_link(self, svc):
        alpha = _mk(svc, project="alpha", title="Alpha task")
        beta = _mk(svc, project="beta", title="Beta task")

        svc.link(alpha.task_id, beta.task_id, "blocks")

        alpha2 = svc.get(alpha.task_id)
        beta2 = svc.get(beta.task_id)
        assert (beta.project, beta.task_number) in alpha2.blocks
        assert (alpha.project, alpha.task_number) in beta2.blocked_by


# ---------------------------------------------------------------------------
# Context log
# ---------------------------------------------------------------------------


class TestContextLog:
    def test_add_context(self, svc):
        t = _mk(svc)
        entry = svc.add_context(t.task_id, "pm", "initial context")
        assert entry.actor == "pm"
        assert entry.text == "initial context"
        assert entry.timestamp is not None

    def test_get_context_ordering(self, svc):
        t = _mk(svc)
        svc.add_context(t.task_id, "pm", "first")
        svc.add_context(t.task_id, "pm", "second")
        svc.add_context(t.task_id, "pm", "third")

        ctx = svc.get_context(t.task_id)
        assert len(ctx) == 3
        assert ctx[0].text == "third"  # most recent first
        assert ctx[1].text == "second"
        assert ctx[2].text == "first"

    def test_get_context_limit(self, svc):
        t = _mk(svc)
        for i in range(5):
            svc.add_context(t.task_id, "pm", f"entry {i}")

        ctx = svc.get_context(t.task_id, limit=2)
        assert len(ctx) == 2

    def test_get_context_since(self, svc):
        t = _mk(svc)
        svc.add_context(t.task_id, "pm", "old entry")

        # Use a timestamp slightly before now for the cutoff
        cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)

        # Small delay to ensure next entries are after cutoff
        time.sleep(0.01)

        # We need entries after cutoff — so set cutoff to before the next ones
        # Actually, let's use the approach of setting cutoff between entries
        # by reading the first entry's timestamp
        ctx_before = svc.get_context(t.task_id)
        assert len(ctx_before) == 1

        # The cutoff is the timestamp of the first entry
        cutoff = ctx_before[0].timestamp

        svc.add_context(t.task_id, "pm", "new entry 1")
        svc.add_context(t.task_id, "pm", "new entry 2")

        ctx = svc.get_context(t.task_id, since=cutoff)
        assert len(ctx) == 2
        for entry in ctx:
            assert entry.timestamp > cutoff
