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
* repeated sweeps don't duplicate plan_missing alerts
* result dict exposes both no_session_alerts and plan_missing_alerts
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pollypm.plugins_builtin.project_planning.plan_presence import (
    BYPASS_LABEL,
    MIN_PLAN_SIZE_BYTES,
    has_acceptable_plan,
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
) -> None:
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
    finally:
        work.close()


def _create_done_approved_plan_task(
    project_path: Path,
    *,
    project_key: str,
    decision: Decision = Decision.APPROVED,
    approved_at: datetime | None = None,
    write_plan_approved_entry: bool = True,
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
        assert "pm project plan proj" in alerts[0].message

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
