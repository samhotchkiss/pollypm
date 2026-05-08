"""Tests for the project-level plan-presence gate — issue #273.

The gate forbids ``task_assignment.sweep`` from delegating
implementation tasks in a project that has no approved plan. These
tests exercise both the predicate (``has_acceptable_plan``) and the
sweep-handler integration.

Coverage matrix:

* predicate True  → sweep delegates normally
* predicate False (no plan.md) → sweep skips, emits one plan_missing alert
* predicate False (plan_project task still draft, not done) → sweep skips
* predicate False (user_approval decision=rejected) → sweep skips
* predicate False (plan.md older than latest backlog task) → sweep skips
* planner's own flow bypasses (plan_project / critique_flow)
* bypass_plan_gate label bypasses
* gate only blocks queued delegation, not review / in_progress re-pings
* repeated sweeps don't duplicate plan_missing alerts
* once the project is delegable again, stale plan_missing alerts clear
* result dict exposes both no_session_alerts and plan_missing_alerts
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pollypm.plan_presence import (
    BYPASS_LABEL,
    MIN_PLAN_SIZE_BYTES,
    has_acceptable_plan,
    plan_approval_task,
    plan_blocked_task_ids,
    task_bypasses_plan_gate,
)
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import _RuntimeServices
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work.models import (
    Decision,
    ExecutionStatus,
    WorkStatus,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Test doubles (mirror the shape from test_task_assignment_fanout.py)
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    handles: list[FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy


@dataclass
class FakeKnownProject:
    key: str
    path: Path
    enforce_plan: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan(project_path: Path, *, size: int = 1500) -> Path:
    """Write a ``docs/plan/plan.md`` with ``size`` bytes of content."""
    plan_path = project_path / "docs" / "plan" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    # Realistic-ish markdown so we're not just writing a block of 'x'.
    body_lines = ["# Forward Plan", "", "## Scope", ""]
    filler = (
        "This project ships the gate-protection layer for the "
        "task-assignment sweep handler, keyed on plan-presence. "
    )
    while sum(len(line) + 1 for line in body_lines) < size:
        body_lines.append(filler)
    plan_path.write_text("\n".join(body_lines), encoding="utf-8")
    return plan_path


def _install_fake_loader(monkeypatch, services_factory):
    monkeypatch.setattr(
        "pollypm.plugins_builtin.task_assignment_notify.handlers.sweep.load_runtime_services",
        lambda *, config_path=None: services_factory(),
    )


def _create_queued_impl_task(
    project_path: Path, *, project_key: str, title: str = "Implement thing",
) -> str:
    """Create + queue one ``standard`` implementation task."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    bus.clear_listeners()
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = work.create(
            title=title,
            description="impl task",
            type="task",
            project=project_key,
            flow_template="standard",
            roles={"worker": "agent-1", "reviewer": "agent-2"},
            priority="normal",
        )
        work.queue(task.task_id, "pm")
        return task.task_id
    finally:
        work.close()


def _advance_task_to_review(
    project_path: Path, *, task_id: str, reviewer: str = "reviewer",
) -> None:
    """Move a standard task to the ``code_review`` node."""
    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        work.claim(task_id, "worker")
        work.node_done(
            task_id,
            "worker",
            work_output={
                "type": "code_change",
                "summary": "implemented the thing",
                "artifacts": [
                    {
                        "kind": "commit",
                        "description": "implementation",
                        "ref": "HEAD",
                    },
                ],
            },
        )
        # Keep the reviewer session-name explicit in the task row so
        # the test doesn't depend on any ambient reviewer naming state.
        work._conn.execute(
            "UPDATE work_tasks SET assignee = ? "
            "WHERE project = ? AND task_number = ?",
            (
                reviewer,
                task_id.rsplit("/", 1)[0],
                int(task_id.rsplit("/", 1)[1]),
            ),
        )
        work._conn.commit()
    finally:
        work.close()


def _create_done_approved_plan_task(
    project_path: Path,
    *,
    project_key: str,
    decision: Decision = Decision.APPROVED,
    approved_at: datetime | None = None,
    write_plan_approved_entry: bool = True,
    emit_completed_at: datetime | None = None,
) -> None:
    """Stamp a plan_project task as done + approved via direct SQL.

    Running the real plan_project flow through all 9 stages inside a
    unit test is overkill — we want to test the gate, not the flow.
    Instead we create the task normally, then write the terminal
    state directly into ``work_tasks`` and ``work_node_executions``.

    ``decision`` lets the caller simulate a rejected outcome for the
    ``rejected`` test case.

    ``approved_at`` overrides the timestamp stamped on both the
    ``user_approval`` execution and the ``plan_approved`` context
    entry. Pass a past ``datetime`` to simulate a stale approval.
    Defaults to "now".

    ``write_plan_approved_entry`` gates whether the explicit
    ``plan_approved`` context entry is written. Pass ``False`` to
    simulate a project that was approved *before* issue #281 shipped,
    so the gate must fall back to the execution's ``completed_at``.

    ``emit_completed_at`` optionally stamps a completed ``emit`` node
    execution at the given time. Pass a datetime > approval to model a
    plan whose architect successfully ran stage 8 — the staleness
    baseline (#1062) extends to ``emit.completed_at`` so impl tasks
    created during emit don't look stale against their own plan.
    """
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    bus.clear_listeners()
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = work.create(
            title=f"Plan {project_key}",
            description="planning",
            type="task",
            project=project_key,
            flow_template="plan_project",
            roles={"architect": "architect"},
            priority="high",
        )
        # Force-advance status to DONE — the sweep gate only cares
        # about ``work_status == 'done'`` + approval decision.
        work._conn.execute(
            "UPDATE work_tasks SET work_status = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.DONE.value, project_key, task.task_number),
        )
        # Insert a user_approval execution row carrying the decision.
        stamp = approved_at or datetime.now(timezone.utc)
        stamp_iso = stamp.isoformat()
        work._conn.execute(
            "INSERT INTO work_node_executions "
            "(task_project, task_number, node_id, visit, status, "
            "decision, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_key,
                task.task_number,
                "user_approval",
                1,
                ExecutionStatus.COMPLETED.value,
                decision.value,
                stamp_iso,
                stamp_iso,
            ),
        )
        # Mirror the work-service behaviour (#281): on approve(), a
        # plan_approved context entry is written. Tests can opt out
        # to simulate pre-fix projects.
        if (
            decision is Decision.APPROVED
            and write_plan_approved_entry
        ):
            work._conn.execute(
                "INSERT INTO work_context_entries "
                "(task_project, task_number, actor, text, "
                "created_at, entry_type) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_key,
                    task.task_number,
                    "user",
                    "plan approved",
                    stamp_iso,
                    "plan_approved",
                ),
            )
        # Optionally stamp the emit execution so the test models a
        # plan whose stage-8 emit completed (#1062 staleness baseline).
        if emit_completed_at is not None:
            emit_iso = emit_completed_at.isoformat()
            work._conn.execute(
                "INSERT INTO work_node_executions "
                "(task_project, task_number, node_id, visit, status, "
                "decision, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    project_key,
                    task.task_number,
                    "emit",
                    1,
                    ExecutionStatus.COMPLETED.value,
                    emit_iso,
                    emit_iso,
                ),
            )
        work._conn.commit()
    finally:
        work.close()


def _create_draft_plan_task(project_path: Path, *, project_key: str) -> None:
    """Create a plan_project task in DRAFT status (no approval yet)."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    bus.clear_listeners()
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        work.create(
            title=f"Plan {project_key}",
            description="planning",
            type="task",
            project=project_key,
            flow_template="plan_project",
            roles={"architect": "architect"},
            priority="high",
        )
    finally:
        work.close()


def _factory_for(
    *, store, session_svc, project_key, project_path, enforce_plan=True,
):
    """Build a ``_RuntimeServices`` factory for a single project."""

    def factory():
        return _RuntimeServices(
            session_service=session_svc,
            state_store=store,
            work_service=None,
            project_root=project_path.parent,
            known_projects=(
                FakeKnownProject(key=project_key, path=project_path),
            ),
            enforce_plan=enforce_plan,
            plan_dir="docs/plan",
        )

    return factory


# ---------------------------------------------------------------------------
# Predicate unit tests
# ---------------------------------------------------------------------------


class TestPredicate:
    """Unit tests for ``has_acceptable_plan`` and ``task_bypasses_plan_gate``."""

    def test_predicate_true_with_plan_and_approval(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        _create_done_approved_plan_task(proj, project_key="proj")

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_predicate_false_when_plan_file_missing(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _create_done_approved_plan_task(proj, project_key="proj")

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is False
        finally:
            work.close()

    def test_predicate_false_when_plan_below_min_size(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        plan = proj / "docs" / "plan" / "plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("# tiny\n", encoding="utf-8")
        assert len(plan.read_text().strip()) < MIN_PLAN_SIZE_BYTES
        _create_done_approved_plan_task(proj, project_key="proj")

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is False
        finally:
            work.close()

    def test_predicate_accepts_plan_whose_children_are_newer(self, tmp_path):
        """Tasks emitted as children of the approved plan_project must
        not count as staleness evidence against that plan. Without this,
        every just-approved plan looks stale relative to its own 18-ish
        emitted implementation tasks and the gate wrongly blocks
        delegation (Notesy regression, 2026-04-23).
        """
        from datetime import datetime, timedelta, timezone

        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)

        # Approve the plan one hour ago.
        approved_at = datetime.now(timezone.utc) - timedelta(hours=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=approved_at,
        )
        # Now create an implementation task (created_at defaults to now
        # — newer than the plan approval) and link it as a child of
        # the plan task. Without the children-exclusion, this should
        # fail the staleness check; with it, the gate stays open.
        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            plan_tasks = [
                t for t in work.list_tasks(project="proj")
                if t.flow_template_id == "plan_project"
            ]
            assert len(plan_tasks) == 1
            plan_task = plan_tasks[0]

            impl = work.create(
                title="Implement thing",
                description="impl task emitted from plan",
                type="task",
                project="proj",
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
            )
            work.queue(impl.task_id, "pm")
            work.link(plan_task.task_id, impl.task_id, "parent")

            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()


    def test_predicate_accepts_docs_project_plan_md_fallback(self, tmp_path):
        """The architect's approval helper writes ``docs/project-plan.md``.
        The gate must accept that path when ``docs/plan/plan.md`` is
        absent — otherwise a project with a fully-approved plan gets
        silently blocked (Notesy regression, 2026-04-23)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        # Write only the approval-helper path; leave docs/plan/plan.md absent.
        plan = proj / "docs" / "project-plan.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        body = "# Forward Plan\n\n## Scope\n\n" + (
            "Canonical forward plan. " * 60
        )
        plan.write_text(body, encoding="utf-8")
        assert (proj / "docs" / "plan" / "plan.md").exists() is False

        _create_done_approved_plan_task(proj, project_key="proj")
        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_predicate_ignores_chat_feedback_artifacts_created_after_approval(self, tmp_path):
        """Inbox/chat artifacts created after plan approval are not backlog.

        Notesy hit this when a ``review_feedback`` chat task created from
        a rejection made the whole project look stale and blocked queued
        worker tasks behind the plan gate.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)

        approved_at = datetime.now(timezone.utc) - timedelta(hours=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=approved_at,
        )

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            work.create(
                title="Rejected proj/7 — Implement thing",
                description="Returned to rework.",
                type="task",
                project="proj",
                flow_template="chat",
                roles={"requester": "reviewer", "operator": "user"},
                priority="high",
                labels=["review_feedback", "task:proj/7", "project:proj"],
            )

            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_plan_blocked_task_ids_surfaces_queued_gate_vetoes(self, tmp_path):
        """Display surfaces can derive ``waiting_on_plan`` from the gate."""
        proj = tmp_path / "proj"
        proj.mkdir()
        blocked_task_id = _create_queued_impl_task(proj, project_key="proj")

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            plan_task = work.create(
                title="Plan proj",
                description="Planning task awaiting approval.",
                type="task",
                project="proj",
                flow_template="plan_project",
                roles={"architect": "architect", "operator": "user"},
                priority="high",
            )
            work.queue(plan_task.task_id, "pm")
            work._conn.execute(
                "UPDATE work_tasks SET work_status = ?, current_node_id = ? "
                "WHERE project = ? AND task_number = ?",
                (
                    WorkStatus.REVIEW.value,
                    "user_approval",
                    "proj",
                    plan_task.task_number,
                ),
            )
            work._conn.commit()

            assert plan_blocked_task_ids("proj", proj, work) == {blocked_task_id}
            approval = plan_approval_task(work, "proj")
            assert approval is not None
            assert approval.task_id == plan_task.task_id
            assert approval.current_node_id == "user_approval"
        finally:
            work.close()

        _write_plan(proj)
        _create_done_approved_plan_task(
            proj,
            project_key="proj",
            approved_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert plan_blocked_task_ids("proj", proj, work) == set()
        finally:
            work.close()


    def test_predicate_false_when_plan_task_rejected(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        _create_done_approved_plan_task(
            proj, project_key="proj", decision=Decision.REJECTED,
        )

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is False
        finally:
            work.close()

    def test_task_bypasses_plan_gate(self):
        @dataclass
        class FakeTask:
            flow_template_id: str = ""
            labels: list[str] = field(default_factory=list)

        assert task_bypasses_plan_gate(FakeTask(flow_template_id="plan_project"))
        assert task_bypasses_plan_gate(FakeTask(flow_template_id="critique_flow"))
        assert task_bypasses_plan_gate(FakeTask(labels=[BYPASS_LABEL]))
        assert not task_bypasses_plan_gate(FakeTask(flow_template_id="standard"))
        assert not task_bypasses_plan_gate(
            FakeTask(flow_template_id="standard", labels=["other_label"])
        )


# ---------------------------------------------------------------------------
# #281 — plan_approved_at timestamp gate
# ---------------------------------------------------------------------------


class TestPlanApprovedAtTimestamp:
    """Unit tests for the #281 timestamp-based freshness check.

    These pin the fix that replaces file mtime with a work-service
    timestamp. Each test drives the predicate directly via
    ``has_acceptable_plan``; the sweep-handler layer is covered by
    ``TestSweepHandlerGateIntegration``.
    """

    def test_gate_accepts_when_plan_approved_at_is_recent(self, tmp_path):
        """plan_approved entry written → gate accepts, even with old file mtime."""
        proj = tmp_path / "proj"
        proj.mkdir()
        plan_path = _write_plan(proj)
        # Force plan.md mtime into the past. Under the old mtime-based
        # gate this would block; under #281 it is irrelevant because
        # the gate reads the context-entry timestamp.
        old = time.time() - 3600
        os.utime(plan_path, (old, old))
        # Queue the impl task first, then approve the plan — so the
        # approval timestamp is strictly ≥ the impl task's created_at.
        _create_queued_impl_task(proj, project_key="proj")
        approved_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=approved_at,
        )

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_gate_rejects_when_plan_not_yet_approved(self, tmp_path):
        """No approved plan_project task → gate rejects."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        # A draft plan task — never approved.
        _create_draft_plan_task(proj, project_key="proj")
        _create_queued_impl_task(proj, project_key="proj")

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is False
        finally:
            work.close()

    def test_gate_rejects_when_plan_approved_at_predates_backlog(self, tmp_path):
        """plan_approved_at < latest backlog task created_at → stale → reject."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        # Approval stamped an hour ago; the impl task below is created
        # "now" so the approval is stale against it.
        stale = datetime.now(timezone.utc) - timedelta(hours=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=stale,
        )
        _create_queued_impl_task(proj, project_key="proj")

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            assert has_acceptable_plan("proj", proj, work) is False
        finally:
            work.close()

    def test_gate_ignores_terminal_tasks_newer_than_plan_approval(self, tmp_path):
        """Done/cancelled rows do not represent active backlog drift."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)

        stale = datetime.now(timezone.utc) - timedelta(hours=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=stale,
        )
        done_task_id = _create_queued_impl_task(
            proj, project_key="proj", title="Already done",
        )
        cancelled_task_id = _create_queued_impl_task(
            proj, project_key="proj", title="Cancelled spam",
        )

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            for task_id, status in (
                (done_task_id, WorkStatus.DONE.value),
                (cancelled_task_id, WorkStatus.CANCELLED.value),
            ):
                project, number = task_id.rsplit("/", 1)
                work._conn.execute(
                    "UPDATE work_tasks SET work_status = ? "
                    "WHERE project = ? AND task_number = ?",
                    (status, project, int(number)),
                )
            work._conn.commit()

            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_gate_falls_back_to_execution_completed_at_for_prefix_projects(
        self, tmp_path,
    ):
        """Pre-#281 project (no plan_approved entry) → fallback accepts.

        A project approved before #281 shipped won't have a
        ``plan_approved`` context entry. The gate falls back to the
        ``user_approval`` execution's ``completed_at`` — otherwise every
        pre-fix project would be stuck behind the gate forever.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        # Queue the impl task first so the approval (stamped after)
        # is strictly ≥ its created_at — the fallback path is what we
        # want to exercise here.
        _create_queued_impl_task(proj, project_key="proj")
        # write_plan_approved_entry=False simulates a pre-#281 task:
        # execution row exists with decision=APPROVED + completed_at,
        # but no plan_approved context entry.
        approved_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        _create_done_approved_plan_task(
            proj,
            project_key="proj",
            approved_at=approved_at,
            write_plan_approved_entry=False,
        )

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            # Sanity: no plan_approved entry exists — we're really
            # testing the fallback.
            task_rows = work._conn.execute(
                "SELECT task_number FROM work_tasks "
                "WHERE project = ? AND flow_template_id = 'plan_project'",
                ("proj",),
            ).fetchall()
            assert len(task_rows) == 1
            number = task_rows[0]["task_number"]
            pae = work._conn.execute(
                "SELECT COUNT(*) AS c FROM work_context_entries "
                "WHERE task_project = ? AND task_number = ? "
                "AND entry_type = 'plan_approved'",
                ("proj", number),
            ).fetchone()
            assert pae["c"] == 0
            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_gate_ignores_file_mtime(self, tmp_path):
        """File mtime is NOT consulted — backdating or future-dating is a no-op.

        Pins the #281 invariant: git operations, editor saves, and the
        planner's own stage-8 emit all perturb mtime. The gate must
        ignore it entirely.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        plan_path = _write_plan(proj)
        # Queue the impl task first so the plan's approval timestamp
        # is strictly newer than the backlog's created_at.
        _create_queued_impl_task(proj, project_key="proj")
        approved_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=approved_at,
        )

        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            # Baseline: default mtime, gate accepts.
            assert has_acceptable_plan("proj", proj, work) is True
            # Future-dated mtime: still accepts.
            future = time.time() + 3600
            os.utime(plan_path, (future, future))
            assert has_acceptable_plan("proj", proj, work) is True
            # Backdated mtime: still accepts (decision is driven by
            # the plan_approved context entry, not the file).
            old = time.time() - 7200
            os.utime(plan_path, (old, old))
            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_emit_completed_extends_staleness_baseline(self, tmp_path):
        """#1062: tasks created during stage-8 emit don't make the plan stale.

        Mirrors the polly_remote / russell / smoketest false-positive:
        the architect parks at user_approval, the user approves, and
        emit then materialises impl tasks. Each impl's ``created_at``
        is strictly later than ``user_approval.completed_at``, so the
        original #281 baseline flagged the plan stale against its own
        output. The fix extends the staleness baseline to
        ``emit.completed_at`` whenever the plan_project task has a
        completed emit execution.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)

        # Approve in the past, emit a few seconds later — same shape
        # the plan_project flow produces in real projects.
        approved_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        emit_completed = approved_at + timedelta(minutes=3)
        _create_done_approved_plan_task(
            proj,
            project_key="proj",
            approved_at=approved_at,
            emit_completed_at=emit_completed,
        )

        # Queue an impl task whose created_at sits *between* approval
        # and emit completion — the architect's emit-burst pattern.
        # ``_create_queued_impl_task`` stamps ``created_at = now`` via
        # the work-service default; for the staleness check we just
        # need it strictly later than approval and earlier-or-equal
        # than emit completion. ``now`` is later than both since the
        # test stamped them in the past, so we instead force the
        # impl's ``created_at`` to an explicit value via direct SQL.
        impl_created_at = approved_at + timedelta(minutes=1)
        _create_queued_impl_task(proj, project_key="proj")
        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            work._conn.execute(
                "UPDATE work_tasks SET created_at = ? "
                "WHERE project = ? AND flow_template_id = 'standard'",
                (impl_created_at.isoformat(), "proj"),
            )
            work._conn.commit()

            # Baseline = max(approval, emit_completed) = emit_completed
            # (5 minutes - 3 minutes = 2 minutes ago) which is still
            # strictly later than impl_created_at (4 minutes ago).
            assert has_acceptable_plan("proj", proj, work) is True
        finally:
            work.close()

    def test_user_added_task_after_emit_completes_still_marks_plan_stale(
        self, tmp_path,
    ):
        """Drift detection survives #1062: tasks created strictly after
        ``emit.completed_at`` still trigger the staleness check.

        The point of #281 is to surface plans that no longer cover the
        backlog. A user adding fresh requirements days after the plan
        wraps up should fail the gate; the #1062 fix only widens the
        baseline to cover the architect's own emit output, not all
        future work.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)

        approved_at = datetime.now(timezone.utc) - timedelta(hours=2)
        emit_completed = approved_at + timedelta(minutes=5)
        _create_done_approved_plan_task(
            proj,
            project_key="proj",
            approved_at=approved_at,
            emit_completed_at=emit_completed,
        )

        # Impl task created an hour after emit completed — drift.
        _create_queued_impl_task(proj, project_key="proj")
        db_path = proj / ".pollypm" / "state.db"
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            drift_at = emit_completed + timedelta(hours=1)
            work._conn.execute(
                "UPDATE work_tasks SET created_at = ? "
                "WHERE project = ? AND flow_template_id = 'standard'",
                (drift_at.isoformat(), "proj"),
            )
            work._conn.commit()

            assert has_acceptable_plan("proj", proj, work) is False
        finally:
            work.close()


# ---------------------------------------------------------------------------
# Sweep-handler integration tests
# ---------------------------------------------------------------------------


class TestSweepHandlerGateIntegration:
    """End-to-end tests — sweep handler + gate + alert emission."""

    def test_approved_plan_allows_delegation(self, tmp_path, monkeypatch):
        """Project with approved plan + impl task → worker gets a ping."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        # Queue the impl task first; approval timestamp sits strictly
        # after its created_at, so the #281 staleness check passes.
        _create_queued_impl_task(proj, project_key="proj")
        _create_done_approved_plan_task(
            proj,
            project_key="proj",
            approved_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        assert result["outcome"] == "swept"
        assert result["projects_scanned"] == 1
        assert result["by_outcome"].get("sent", 0) == 1
        assert result["plan_missing_alerts"] == 0
        assert result["no_session_alerts"] == 0
        # Worker got pinged.
        assert session_svc.sent
        assert session_svc.sent[0][0] == "worker-proj"

        store.close()

    def test_no_plan_skips_all_impl_tasks_and_emits_alert(
        self, tmp_path, monkeypatch,
    ):
        """No plan.md → queued impl tasks produce zero pings + one alert."""
        proj = tmp_path / "proj"
        proj.mkdir()
        # Two queued impl tasks, no plan.
        _create_queued_impl_task(proj, project_key="proj", title="Thing A")
        _create_queued_impl_task(proj, project_key="proj", title="Thing B")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        assert result["outcome"] == "swept"
        assert result["by_outcome"].get("sent", 0) == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 2
        # Exactly one plan_missing alert for this project.
        assert result["plan_missing_alerts"] == 1
        # No pings went out.
        assert session_svc.sent == []
        # Alert landed in the store with the right shape.
        alerts = [a for a in store.open_alerts() if a.alert_type == "plan_missing"]
        assert len(alerts) == 1
        assert alerts[0].session_name == "plan_gate-proj"
        # #866 + #1029: alert text surfaces both the ``pm project plan``
        # CLI verb (lower-friction path for users reading ``pm alerts``
        # cold) and the in-cockpit affordance (press ``c`` on the
        # project Plan card). Both must be present so future edits
        # can't silently drop either.
        assert "pm project plan" in alerts[0].message
        assert "press" in alerts[0].message.lower()

        store.close()

    def test_draft_plan_task_blocks_delegation(self, tmp_path, monkeypatch):
        """plan.md exists but the plan_project task is still draft (not done)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        _create_draft_plan_task(proj, project_key="proj")
        _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        assert result["by_outcome"].get("sent", 0) == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 1
        assert result["plan_missing_alerts"] == 1
        assert session_svc.sent == []

        store.close()

    def test_rejected_plan_blocks_delegation(self, tmp_path, monkeypatch):
        """plan.md exists but user_approval was rejected."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        _create_done_approved_plan_task(
            proj, project_key="proj", decision=Decision.REJECTED,
        )
        _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        assert result["by_outcome"].get("sent", 0) == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 1
        assert result["plan_missing_alerts"] == 1

        store.close()

    def test_stale_plan_blocks_delegation(self, tmp_path, monkeypatch):
        """plan_approved_at older than latest backlog task → blocks.

        (#281) Freshness is keyed off the approval timestamp, not the
        plan.md mtime. Stamp an approval from an hour ago; the queued
        impl task's ``created_at`` is "now", so the plan is stale.
        """
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_plan(proj)
        # Approval timestamp well before any backlog task is created.
        stale = datetime.now(timezone.utc) - timedelta(hours=1)
        _create_done_approved_plan_task(
            proj, project_key="proj", approved_at=stale,
        )
        _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        assert result["by_outcome"].get("sent", 0) == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 1
        assert result["plan_missing_alerts"] == 1

        store.close()

    def test_plan_project_flow_bypasses_gate(self, tmp_path, monkeypatch):
        """A queued plan_project task runs without the gate (planner produces the plan)."""
        proj = tmp_path / "proj"
        proj.mkdir()
        # No plan.md at all — but a queued plan_project task must still
        # be considered by the sweep (i.e. the plan gate must not block it).
        db_path = proj / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        bus.clear_listeners()
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            task = work.create(
                title="Plan proj",
                description="Run the planning pipeline for proj.",
                type="task",
                project="proj",
                flow_template="plan_project",
                roles={"architect": "architect"},
                priority="high",
            )
            work.queue(task.task_id, "pm")
        finally:
            work.close()

        store = StateStore(tmp_path / "workspace_state.db")
        # No handles — we're only asserting the gate let the task
        # through to the notify path. Session resolution for the
        # architect role is a separate concern owned by the resolver.
        session_svc = FakeSessionService(handles=[])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        # Planner flow is exempt — gate does not block it.
        assert result["plan_missing_alerts"] == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 0
        # The task reached the notify path; it was *considered* for
        # delegation (the whole point of the bypass). Whether a session
        # exists to receive the ping is a separate concern.
        assert result["considered"] >= 1

        store.close()

    def test_bypass_label_bypasses_gate(self, tmp_path, monkeypatch):
        """A standard task with ``bypass_plan_gate`` label runs without a plan."""
        proj = tmp_path / "proj"
        proj.mkdir()
        db_path = proj / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        bus.clear_listeners()
        work = SQLiteWorkService(db_path=db_path, project_path=proj)
        try:
            task = work.create(
                title="Emergency hotfix",
                description="Production is on fire; skip the planner.",
                type="task",
                project="proj",
                flow_template="standard",
                labels=[BYPASS_LABEL],
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="high",
            )
            work.queue(task.task_id, "pm")
        finally:
            work.close()

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        result = task_assignment_sweep_handler({})

        assert result["plan_missing_alerts"] == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 0
        assert result["by_outcome"].get("sent", 0) == 1

        store.close()

    def test_repeated_sweeps_do_not_duplicate_plan_missing_alerts(
        self, tmp_path, monkeypatch,
    ):
        """Upsert semantics — two sweeps, still one open plan_missing row."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        task_assignment_sweep_handler({})
        first = [a for a in store.open_alerts() if a.alert_type == "plan_missing"]
        assert len(first) == 1

        task_assignment_sweep_handler({})
        second = [a for a in store.open_alerts() if a.alert_type == "plan_missing"]
        assert len(second) == 1
        assert first[0].alert_id == second[0].alert_id

        store.close()

    def test_plan_missing_alert_clears_once_project_is_delegable(
        self, tmp_path, monkeypatch,
    ):
        """A stale plan-gate alert should close once the queued task is
        no longer blocked by the gate."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        first = task_assignment_sweep_handler({})
        assert first["plan_missing_alerts"] == 1
        alerts = [a for a in store.open_alerts() if a.alert_type == "plan_missing"]
        assert len(alerts) == 1
        assert alerts[0].session_name == "plan_gate-proj"

        _write_plan(proj)
        _create_done_approved_plan_task(
            proj,
            project_key="proj",
            approved_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )

        second = task_assignment_sweep_handler({})
        assert second["plan_missing_alerts"] == 0
        assert second["by_outcome"].get("sent", 0) == 1
        assert session_svc.sent and session_svc.sent[-1][0] == "worker-proj"
        assert [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ] == []

        store.close()

    def test_review_reping_bypasses_plan_gate_and_clears_stale_alert(
        self, tmp_path, monkeypatch,
    ):
        """A task already parked in review should still ping the reviewer
        even if the project has no plan, and any old queued-task alert
        should be cleared."""
        proj = tmp_path / "proj"
        proj.mkdir()
        task_id = _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("reviewer")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
        ))

        first = task_assignment_sweep_handler({})
        assert first["plan_missing_alerts"] == 1
        assert [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ]

        _advance_task_to_review(proj, task_id=task_id)

        second = task_assignment_sweep_handler({})
        assert second["plan_missing_alerts"] == 0
        assert second["by_outcome"].get("sent", 0) == 1
        assert session_svc.sent and session_svc.sent[-1][0] == "reviewer"
        assert "Review needed" in session_svc.sent[-1][1]
        assert task_id in session_svc.sent[-1][1]
        assert [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ] == []

        store.close()

    def test_result_dict_includes_both_alert_counters(
        self, tmp_path, monkeypatch,
    ):
        """Two projects, one with no plan, one with no session — both counters populate."""
        # Project A: has plan, has impl task, but no session → no_session
        proj_a = tmp_path / "proj_a"
        proj_a.mkdir()
        _write_plan(proj_a)
        # Queue impl task first so the approval timestamp is strictly
        # newer than the backlog's created_at (#281 staleness check).
        _create_queued_impl_task(proj_a, project_key="alpha")
        _create_done_approved_plan_task(
            proj_a,
            project_key="alpha",
            approved_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )

        # Project B: no plan, has impl task → plan_missing
        proj_b = tmp_path / "proj_b"
        proj_b.mkdir()
        _create_queued_impl_task(proj_b, project_key="beta")

        store = StateStore(tmp_path / "workspace_state.db")
        # No handles at all — project A trips no_session, project B trips plan_missing.
        session_svc = FakeSessionService(handles=[])

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=None,
                project_root=tmp_path,
                known_projects=(
                    FakeKnownProject(key="alpha", path=proj_a),
                    FakeKnownProject(key="beta", path=proj_b),
                ),
                enforce_plan=True,
                plan_dir="docs/plan",
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        assert result["outcome"] == "swept"
        assert result["no_session_alerts"] == 1
        assert result["plan_missing_alerts"] == 1
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 1
        assert result["by_outcome"].get("no_session", 0) >= 1

        store.close()

    def test_enforce_plan_false_disables_gate(self, tmp_path, monkeypatch):
        """With ``enforce_plan=False``, the gate is skipped entirely."""
        proj = tmp_path / "proj"
        proj.mkdir()
        # No plan.md — would normally block.
        _create_queued_impl_task(proj, project_key="proj")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])
        _install_fake_loader(monkeypatch, _factory_for(
            store=store, session_svc=session_svc,
            project_key="proj", project_path=proj,
            enforce_plan=False,
        ))

        result = task_assignment_sweep_handler({})

        # Gate disabled → worker gets pinged even without a plan.
        assert result["plan_missing_alerts"] == 0
        assert result["by_outcome"].get("sent", 0) == 1
        assert session_svc.sent and session_svc.sent[0][0] == "worker-proj"

        store.close()

    def test_workspace_root_pass_applies_gate_for_registered_project(
        self, tmp_path, monkeypatch,
    ):
        """Issue #938: a queued task stored in the workspace-root DB
        whose ``project`` matches a registered project must honour the
        plan gate during the workspace-root sweep pass. Without the
        fix, the sweep pings the worker before the per-project auto-
        claim pass blocks the same task with ``plan_missing``."""
        # Project with no plan — gate must close.
        proj = tmp_path / "proj"
        proj.mkdir()

        # Workspace-root DB lives at the workspace root, not inside
        # the project. The queued task is created with ``project="proj"``
        # so the workspace-root sweep can match it against the
        # registered project's filesystem anchor.
        workspace_db = tmp_path / "workspace.db"
        bus.clear_listeners()
        ws_work = SQLiteWorkService(db_path=workspace_db, project_path=tmp_path)
        try:
            task = ws_work.create(
                title="Impl without plan",
                description="x",
                type="task",
                project="proj",
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
            )
            ws_work.queue(task.task_id, "pm")
        finally:
            ws_work.close()

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[FakeHandle("worker-proj")])

        def _factory():
            # Reopen so the sweep handler owns the connection lifecycle.
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=SQLiteWorkService(
                    db_path=workspace_db, project_path=tmp_path,
                ),
                project_root=tmp_path,
                known_projects=(
                    FakeKnownProject(key="proj", path=proj),
                ),
                enforce_plan=True,
                plan_dir="docs/plan",
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        # Gate must block the workspace-root pass too — no ping should
        # land for a plan-gated task.
        assert session_svc.sent == []
        assert result["by_outcome"].get("sent", 0) == 0
        assert result["by_outcome"].get("skipped_plan_missing", 0) >= 1
        assert result["plan_missing_alerts"] >= 1

        store.close()

    def test_per_project_enforce_plan_false_disables_gate_for_one_project(
        self, tmp_path, monkeypatch,
    ):
        """Per-project ``enforce_plan=False`` overrides the global default.

        Two projects, both missing plans. Global ``enforce_plan=True``
        keeps the gate active; the per-project override on ``alpha``
        unblocks it while ``beta`` still trips ``plan_missing``. Mirrors
        Sam's media-project ask: bypass the gate for one project without
        flipping the global default.
        """
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        _create_queued_impl_task(proj_a, project_key="alpha")

        proj_b = tmp_path / "beta"
        proj_b.mkdir()
        _create_queued_impl_task(proj_b, project_key="beta")

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(
            handles=[FakeHandle("worker-alpha"), FakeHandle("worker-beta")],
        )

        def _factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=None,
                project_root=tmp_path,
                known_projects=(
                    FakeKnownProject(
                        key="alpha", path=proj_a, enforce_plan=False,
                    ),
                    FakeKnownProject(key="beta", path=proj_b),
                ),
                enforce_plan=True,
                plan_dir="docs/plan",
            )

        _install_fake_loader(monkeypatch, _factory)

        result = task_assignment_sweep_handler({})

        # alpha → gate skipped → worker pinged.
        assert any(name == "worker-alpha" for name, _ in session_svc.sent)
        # beta → gate still enforced → plan_missing alert.
        assert result["plan_missing_alerts"] == 1
        assert result["by_outcome"].get("skipped_plan_missing", 0) == 1

        store.close()


class TestPlanMissingAlertStability1524:
    """#1524 — plan_missing alert stays stable across consecutive ticks
    when the canonical workspace DB carries the queued task and the
    per-project legacy DB has been drained (post-#1519). Pre-fix, the
    sweep handler's per-project fallback at line 2089-2090 cleared the
    alert on every other tick because ``plan_missing_projects`` was
    populated as a side effect of the auto-claim loop, which only ran
    for projects with auto-claim enabled. The pre-compute step in
    ``_task_assignment_sweep_body`` makes the set deterministic.
    """

    def _setup_workspace_db_no_per_project(
        self, *, tmp_path, project_key, queued_label=None,
    ):
        """Workspace DB carries a queued task; the per-project DB is absent.

        Mirrors the post-#1519 coffeeboardnm shape: canonical workspace
        ``state.db`` has the queued advisor_review (here, a standard
        impl task — same gate behaviour) and the per-project
        ``state.db`` has been drained / never created. Returns the
        workspace_db path and the project_path.
        """
        proj_path = tmp_path / project_key
        proj_path.mkdir()
        # Ensure the per-project DB does NOT exist — that's the post-
        # #1519 drain shape we want to reproduce.
        assert not (proj_path / ".pollypm" / "state.db").exists()

        workspace_db = tmp_path / ".pollypm" / "state.db"
        workspace_db.parent.mkdir(parents=True, exist_ok=True)
        bus.clear_listeners()
        ws_work = SQLiteWorkService(db_path=workspace_db, project_path=tmp_path)
        try:
            task = ws_work.create(
                title="Queued impl on canonical DB",
                description="x",
                type="task",
                project=project_key,
                flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
                labels=[queued_label] if queued_label else None,
            )
            ws_work.queue(task.task_id, "pm")
        finally:
            ws_work.close()
        return workspace_db, proj_path

    def _factory_with_workspace_db(
        self, *, store, session_svc, workspace_db, tmp_path,
        project_key, project_path, auto_claim=True,
    ):
        def factory():
            return _RuntimeServices(
                session_service=session_svc,
                state_store=store,
                work_service=SQLiteWorkService(
                    db_path=workspace_db, project_path=tmp_path,
                ),
                project_root=tmp_path,
                known_projects=(
                    FakeKnownProject(key=project_key, path=project_path),
                ),
                enforce_plan=True,
                plan_dir="docs/plan",
                auto_claim=auto_claim,
            )

        return factory

    def test_alert_id_stable_across_consecutive_ticks_post_drain(
        self, tmp_path, monkeypatch,
    ):
        """Two sweeps in a row, no state change, no per-project DB.

        Pre-#1524: the alert ID changed each tick (clear → re-emit
        cycle). Post-fix: same row stays open, ``upsert_alert`` just
        refreshes it.
        """
        workspace_db, proj_path = self._setup_workspace_db_no_per_project(
            tmp_path=tmp_path, project_key="coffeeboardnm",
        )

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[])  # no sessions wired

        _install_fake_loader(monkeypatch, self._factory_with_workspace_db(
            store=store, session_svc=session_svc,
            workspace_db=workspace_db, tmp_path=tmp_path,
            project_key="coffeeboardnm", project_path=proj_path,
        ))

        first = task_assignment_sweep_handler({})
        first_alerts = [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ]
        assert len(first_alerts) == 1, first
        assert first_alerts[0].session_name == "plan_gate-coffeeboardnm"
        first_alert_id = first_alerts[0].alert_id

        # Second tick — same state.
        second = task_assignment_sweep_handler({})
        second_alerts = [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ]
        # Exactly one open alert, and it's the SAME row as before (the
        # row was refreshed in place, not closed-and-re-opened).
        assert len(second_alerts) == 1, second
        assert second_alerts[0].alert_id == first_alert_id, (
            "plan_missing alert was cleared and re-emitted between ticks; "
            f"expected stable alert_id {first_alert_id}, got "
            f"{second_alerts[0].alert_id}. This is the #1524 banner-flash "
            "regression."
        )
        # Result counter still records the project as plan-missing.
        assert second["plan_missing_alerts"] >= 1

        store.close()

    def test_alert_stable_when_auto_claim_disabled(
        self, tmp_path, monkeypatch,
    ):
        """Pre-fix, the auto-claim loop was the source-of-truth for
        ``plan_missing_projects``; if auto-claim was disabled (global
        flag off, or per-project ``auto_claim=false``), the project
        never landed in the set and the per-project fallback cleared
        the alert on every tick. The pre-compute step is independent
        of auto-claim, so the alert stays stable regardless.
        """
        workspace_db, proj_path = self._setup_workspace_db_no_per_project(
            tmp_path=tmp_path, project_key="coffeeboardnm",
        )

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[])

        _install_fake_loader(monkeypatch, self._factory_with_workspace_db(
            store=store, session_svc=session_svc,
            workspace_db=workspace_db, tmp_path=tmp_path,
            project_key="coffeeboardnm", project_path=proj_path,
            auto_claim=False,  # globally disabled — pre-fix this hid the project
        ))

        first = task_assignment_sweep_handler({})
        first_alerts = [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ]
        assert len(first_alerts) == 1, first
        first_alert_id = first_alerts[0].alert_id

        second = task_assignment_sweep_handler({})
        second_alerts = [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ]
        assert len(second_alerts) == 1, second
        assert second_alerts[0].alert_id == first_alert_id, (
            "plan_missing alert flapped when auto-claim was disabled; "
            "Part A precompute should not depend on the auto-claim path."
        )

        store.close()

    def test_no_alert_when_no_queued_blocked_work(
        self, tmp_path, monkeypatch,
    ):
        """A project with no plan but no queued non-bypass tasks does
        not get a plan_missing alert — the alert isn't actionable when
        nothing is waiting. (Sanity check for the precompute filter.)
        """
        proj_path = tmp_path / "fresh"
        proj_path.mkdir()

        workspace_db = tmp_path / ".pollypm" / "state.db"
        workspace_db.parent.mkdir(parents=True, exist_ok=True)
        # Workspace DB exists but carries no tasks for this project.
        bus.clear_listeners()
        ws_work = SQLiteWorkService(db_path=workspace_db, project_path=tmp_path)
        ws_work.close()

        store = StateStore(tmp_path / "workspace_state.db")
        session_svc = FakeSessionService(handles=[])

        _install_fake_loader(monkeypatch, self._factory_with_workspace_db(
            store=store, session_svc=session_svc,
            workspace_db=workspace_db, tmp_path=tmp_path,
            project_key="fresh", project_path=proj_path,
        ))

        result = task_assignment_sweep_handler({})

        plan_missing = [
            a for a in store.open_alerts() if a.alert_type == "plan_missing"
        ]
        assert plan_missing == []
        assert result.get("plan_missing_alerts", 0) == 0

        store.close()

    def test_compute_plan_missing_projects_helper_is_deterministic(
        self, tmp_path,
    ):
        """Unit-level: the helper returns the same mapping for the same
        inputs across repeated calls — no hidden mutation of inputs.
        """
        from pollypm.plan_presence import compute_plan_missing_projects

        proj_path = tmp_path / "demo"
        proj_path.mkdir()
        workspace_db = tmp_path / ".pollypm" / "state.db"
        workspace_db.parent.mkdir(parents=True, exist_ok=True)
        bus.clear_listeners()
        ws_work = SQLiteWorkService(db_path=workspace_db, project_path=tmp_path)
        try:
            task = ws_work.create(
                title="x", description="x", type="task",
                project="demo", flow_template="standard",
                roles={"worker": "agent-1", "reviewer": "agent-2"},
                priority="normal",
            )
            ws_work.queue(task.task_id, "pm")
        finally:
            ws_work.close()

        def _opener(_project):
            return SQLiteWorkService(
                db_path=workspace_db, project_path=tmp_path,
            )

        projects = (FakeKnownProject(key="demo", path=proj_path),)
        first = compute_plan_missing_projects(
            projects, work_service_for_project=_opener,
        )
        second = compute_plan_missing_projects(
            projects, work_service_for_project=_opener,
        )
        # Both calls report the project as plan-missing with a concrete
        # task_id. Same inputs → same shape (the example task may be
        # any queued non-bypass task; we only assert it's non-empty).
        assert set(first.keys()) == {"demo"}
        assert set(second.keys()) == {"demo"}
        assert first["demo"] is not None
        assert second["demo"] is not None
