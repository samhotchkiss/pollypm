"""Regression tests for #1999 — reaper demote + 3-strike escalation.

Sam-approved policy:

* 1st reap on a task → demote silently (queue, no inbox noise)
* 2nd reap on a task → demote silently
* 3rd+ reap on a task → demote AND escalate to the inbox

The ``reap_count`` counter is persisted on ``work_tasks.reap_count`` so
the threshold survives cockpit restarts. These tests assert the
end-to-end shape:

* :func:`bump_reap_count_and_demote` atomically demotes the task back to
  ``queued`` and returns the post-increment count.
* The reaper integration emits a ``notify``-labelled inbox task only on
  the 3rd+ reap (titled ``Task <id> reaped N times — likely systemic issue``).
* Subsequent reaps of an already-terminal task are no-ops (defensive
  guard against clobbering a cancelled task).

The tests exercise the pg backend via the shared ``pg_work_service`` /
``pg_schema_pool`` fixtures so the counter lands in real pg rows. The
filesystem half of the reaper (marker unlink, tmux probe) is covered
by the legacy ``tests/test_worker_marker_reaper.py``; here we focus on
the new mutation + escalation path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pollypm.work.worker_marker_reaper import (
    REAP_ESCALATION_THRESHOLD,
    ReapedMarker,
    _demote_and_maybe_escalate,
)
from pollypm.storage.work_task_state import bump_reap_count_and_demote


def _make_inprogress_task(svc, *, project: str = "demo", title: str = "T") -> str:
    """Create a task and drive it to ``in_progress``.

    Mirrors the production lifecycle for a task that a worker is
    actively claiming. The reaper's #1999 branch only triggers when the
    task is non-terminal — ``in_progress`` is the most common case in
    the wild (see issue evidence: 10× in_progress, 5× review, 5×
    queued).
    """
    task = svc.create(
        title=title,
        description="seed",
        type="task",
        project=project,
        flow_template="default",
        roles={"worker": "alice"},
        priority="normal",
        created_by="seed",
    )
    svc.queue(task.task_id, actor="seed")
    svc.claim(task.task_id, actor="alice")
    return task.task_id


def _fake_config() -> Any:
    """Minimal config object the reaper helpers consult."""

    class _Cfg:
        # No projects map needed — the inbox-emit path resolves the
        # work-service via the pg pool, not the config's projects dict.
        projects: dict = {}

        class storage:  # noqa: N801 — match attribute shape
            backend = "postgres"

        class project:  # noqa: N801
            workspace_root = None
            base_dir = None
            tmux_session = "test-pm"

    return _Cfg()


# ---------------------------------------------------------------------------
# bump_reap_count_and_demote
# ---------------------------------------------------------------------------


class TestBumpReapCountAndDemote:
    def test_first_bump_demotes_and_returns_one(self, pg_work_service):
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, num = task_id.split("/")
        count = bump_reap_count_and_demote(
            project_key=project, task_number=int(num), config=None,
        )
        assert count == 1
        # Task is back in queued, assignee cleared.
        task = svc.get(task_id)
        assert task.work_status.value == "queued"
        assert task.assignee is None

    def test_second_bump_returns_two(self, pg_work_service):
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, num = task_id.split("/")
        bump_reap_count_and_demote(
            project_key=project, task_number=int(num), config=None,
        )
        # Simulate re-claim then re-reap.
        svc.claim(task_id, actor="alice")
        count = bump_reap_count_and_demote(
            project_key=project, task_number=int(num), config=None,
        )
        assert count == 2

    def test_third_bump_returns_three(self, pg_work_service):
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, num = task_id.split("/")
        for _ in range(2):
            bump_reap_count_and_demote(
                project_key=project, task_number=int(num), config=None,
            )
            svc.claim(task_id, actor="alice")
        count = bump_reap_count_and_demote(
            project_key=project, task_number=int(num), config=None,
        )
        assert count == 3
        assert count >= REAP_ESCALATION_THRESHOLD

    def test_terminal_task_does_not_bump(self, pg_work_service):
        """A cancelled task must not be demoted back to queued."""
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, num = task_id.split("/")
        svc.cancel(task_id, actor="user", reason="not needed")
        count = bump_reap_count_and_demote(
            project_key=project, task_number=int(num), config=None,
        )
        assert count is None
        task = svc.get(task_id)
        assert task.work_status.value == "cancelled"

    def test_missing_row_returns_none(self, pg_work_service):
        count = bump_reap_count_and_demote(
            project_key="ghost", task_number=999, config=None,
        )
        assert count is None


# ---------------------------------------------------------------------------
# _demote_and_maybe_escalate — three-strike escalation policy
# ---------------------------------------------------------------------------


def _reaped_marker_for(task_id: str, *, project_key: str = "demo") -> ReapedMarker:
    project, num = task_id.split("/")
    return ReapedMarker(
        project_key=project_key,
        window_name=f"task-{project}-{num}",
        marker_path=Path(f"/tmp/dummy-marker-{project}-{num}.fresh"),
        reason=f"tmux window missing for non-terminal task (status='in_progress')",
        task_project=project,
        task_number=int(num),
        window_missing_for_non_terminal=True,
    )


def _count_escalation_inbox_tasks(svc, *, project: str, task_id: str) -> int:
    dedupe_label = f"reap_repeat:{task_id}"
    found = 0
    for status in ("queued", "in_progress", "draft"):
        for task in svc.list_tasks(project=project, work_status=status):
            labels = getattr(task, "labels", []) or []
            if dedupe_label in labels:
                found += 1
    return found


class TestThreeStrikeEscalation:
    def test_first_reap_demotes_no_inbox_item(self, pg_work_service):
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, _ = task_id.split("/")
        marker = _reaped_marker_for(task_id)
        config = _fake_config()

        _demote_and_maybe_escalate(config=config, decision=marker)

        task = svc.get(task_id)
        assert task.work_status.value == "queued"
        assert _count_escalation_inbox_tasks(
            svc, project=project, task_id=task_id,
        ) == 0

    def test_second_reap_still_no_inbox_item(self, pg_work_service):
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, _ = task_id.split("/")
        marker = _reaped_marker_for(task_id)
        config = _fake_config()

        _demote_and_maybe_escalate(config=config, decision=marker)
        # Re-claim then re-reap to simulate the wild lifecycle.
        svc.claim(task_id, actor="alice")
        _demote_and_maybe_escalate(config=config, decision=marker)

        assert _count_escalation_inbox_tasks(
            svc, project=project, task_id=task_id,
        ) == 0
        task = svc.get(task_id)
        assert task.work_status.value == "queued"

    def test_third_reap_demotes_and_emits_inbox_item(self, pg_work_service):
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, _ = task_id.split("/")
        marker = _reaped_marker_for(task_id)
        config = _fake_config()

        for _ in range(2):
            _demote_and_maybe_escalate(config=config, decision=marker)
            svc.claim(task_id, actor="alice")
        # Third reap → demote AND escalate.
        _demote_and_maybe_escalate(config=config, decision=marker)

        task = svc.get(task_id)
        assert task.work_status.value == "queued"

        # An inbox task carrying the dedupe label exists with the
        # canonical "reaped N times — likely systemic issue" title.
        dedupe_label = f"reap_repeat:{task_id}"
        matched = []
        for status in ("queued", "in_progress", "draft"):
            for inbox_task in svc.list_tasks(
                project=project, work_status=status,
            ):
                labels = getattr(inbox_task, "labels", []) or []
                if dedupe_label in labels:
                    matched.append(inbox_task)
        assert len(matched) == 1, (
            f"expected 1 escalation inbox task, got {len(matched)}"
        )
        escalation = matched[0]
        assert "reaped 3 times" in escalation.title
        assert "likely systemic issue" in escalation.title
        labels = list(getattr(escalation, "labels", []) or [])
        assert "notify" in labels
        assert "reap_escalation" in labels

    def test_non_terminal_marker_branch_is_required(self, pg_work_service):
        """Markers reaped for other reasons must NOT touch reap_count."""
        svc = pg_work_service
        task_id = _make_inprogress_task(svc)
        project, num = task_id.split("/")

        terminal_marker = ReapedMarker(
            project_key="demo",
            window_name=f"task-{project}-{num}",
            marker_path=Path("/tmp/dummy.fresh"),
            reason="task in terminal status 'done'",
            task_project=project,
            task_number=int(num),
            window_missing_for_non_terminal=False,  # critical
        )
        config = _fake_config()

        _demote_and_maybe_escalate(config=config, decision=terminal_marker)

        # Task should still be in_progress — the helper short-circuited.
        task = svc.get(task_id)
        assert task.work_status.value == "in_progress"
