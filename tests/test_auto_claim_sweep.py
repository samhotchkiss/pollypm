"""Tests for the #768 auto-claim layer on the task_assignment sweep.

The sweep now claims queued worker-role tasks on behalf of the user
(bounded by ``max_concurrent_per_project``) whenever the plan-gate is
open. The self-heal layer unclaims in-progress tasks whose tmux window
is missing so a crashed worker doesn't permanently lock its task in
``in_progress``.

These tests exercise the pure-Python branches of the auto-claim
helpers — no real tmux sessions are started. The Notesy regression is
specifically pinned: a project with an approved plan + queued tasks +
zero active workers auto-claims one per sweep tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    _auto_claim_enabled_for_project,
    _auto_claim_next,
    _max_concurrent_for_project,
    _open_project_work_service,
    _recover_dead_claims,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import _RuntimeServices
from pollypm.work import task_assignment as bus
from pollypm.work.models import (
    Artifact,
    ArtifactKind,
    ExecutionStatus,
    OutputType,
    WorkOutput,
    WorkStatus,
)
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------


class _FakeProject:
    def __init__(
        self,
        *,
        key: str = "proj",
        path: Path = Path("/tmp/proj"),
        auto_claim: bool | None = None,
        max_concurrent_workers: int | None = None,
    ) -> None:
        self.key = key
        self.path = path
        self.auto_claim = auto_claim
        self.max_concurrent_workers = max_concurrent_workers


class _FakeTmux:
    def __init__(self, windows: list[object] | None = None) -> None:
        self._windows = list(windows or [])

    def list_windows(self, session_name: str) -> list[object]:
        return list(self._windows)


class _FakeSessionService:
    def __init__(self, windows: list[object] | None = None) -> None:
        self.tmux = _FakeTmux(windows)

    def storage_closet_session_name(self) -> str:
        return "pollypm-storage-closet"


def _svc_defaults(**overrides) -> _RuntimeServices:
    defaults = dict(
        session_service=None, state_store=None, work_service=None,
        project_root=Path("/tmp"),
        auto_claim=True, max_concurrent_per_project=2,
    )
    defaults.update(overrides)
    return _RuntimeServices(**defaults)


def test_auto_claim_enabled_follows_defaults() -> None:
    assert _auto_claim_enabled_for_project(_svc_defaults(), _FakeProject()) is True


def test_auto_claim_disabled_by_global_flag() -> None:
    svc = _svc_defaults(auto_claim=False)
    assert _auto_claim_enabled_for_project(svc, _FakeProject()) is False


def test_auto_claim_disabled_per_project() -> None:
    """Explicit per-project ``auto_claim = false`` wins over global True."""
    project = _FakeProject(auto_claim=False)
    assert _auto_claim_enabled_for_project(_svc_defaults(), project) is False


def test_auto_claim_per_project_none_defers_to_global() -> None:
    """``auto_claim=None`` on the project means "use the global default"."""
    project = _FakeProject(auto_claim=None)
    assert _auto_claim_enabled_for_project(_svc_defaults(auto_claim=True), project) is True
    assert _auto_claim_enabled_for_project(_svc_defaults(auto_claim=False), project) is False


def test_max_concurrent_per_project_override_wins() -> None:
    project = _FakeProject(max_concurrent_workers=5)
    assert _max_concurrent_for_project(_svc_defaults(), project) == 5


def test_max_concurrent_default_when_no_override() -> None:
    svc = _svc_defaults(max_concurrent_per_project=3)
    assert _max_concurrent_for_project(svc, _FakeProject()) == 3


def test_max_concurrent_floors_at_one() -> None:
    """A zero / negative config value shouldn't silently disable claims —
    ``auto_claim=false`` is the documented off switch. Floor at 1."""
    svc = _svc_defaults(max_concurrent_per_project=0)
    assert _max_concurrent_for_project(svc, _FakeProject()) == 1


def test_open_project_work_service_wires_session_manager(tmp_path: Path, monkeypatch) -> None:
    """Per-project sweep DBs must provision workers, not only claim in DB."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    seed = SQLiteWorkService(db_path=db_path, project_path=project_path)
    seed.close()

    wired: dict[str, object] = {}

    class DummySessionManager:
        def __init__(self, *args, **kwargs) -> None:
            wired["args"] = args
            wired["kwargs"] = kwargs

    monkeypatch.setattr(
        "pollypm.session_services.create_tmux_client",
        lambda: "tmux-client",
    )
    monkeypatch.setattr(
        "pollypm.work.session_manager.SessionManager",
        DummySessionManager,
    )

    services = _svc_defaults(
        session_service=object(),
        config=SimpleNamespace(project=SimpleNamespace(tmux_session="pollypm")),
        storage_closet_name="pollypm-storage-closet",
    )
    project = _FakeProject(key="proj", path=project_path)

    svc = _open_project_work_service(project, services)
    try:
        assert svc is not None
        assert type(getattr(svc, "_session_mgr", None)).__name__ == "DummySessionManager"
        assert wired["kwargs"]["project_path"] == project_path
        assert wired["kwargs"]["storage_closet_name"] == "pollypm-storage-closet"
        assert wired["kwargs"]["session_service"] is services.session_service
    finally:
        if svc is not None:
            svc.close()


# ---------------------------------------------------------------------------
# Full auto-claim integration against a real work service
# ---------------------------------------------------------------------------


def _write_plan(project_path: Path) -> None:
    plan_path = project_path / "docs" / "project-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Plan\n" + "body " * 200, encoding="utf-8")


def _seed_approved_plan(
    project_path: Path, project_key: str, *, approved_at: datetime | None = None,
) -> None:
    """Stamp a done+approved plan_project task via direct SQL so the
    gate sees an approved plan without running the full flow."""
    from pollypm.work.models import Decision, ExecutionStatus

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title="Plan project",
            description="planning",
            type="task",
            project=project_key,
            flow_template="plan_project",
            roles={"architect": "architect"},
            priority="high",
        )
        stamp = approved_at or datetime.now(timezone.utc) - timedelta(minutes=5)
        stamp_iso = stamp.isoformat()
        svc._conn.execute(
            "UPDATE work_tasks SET work_status = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.DONE.value, project_key, task.task_number),
        )
        svc._conn.execute(
            "INSERT INTO work_node_executions "
            "(task_project, task_number, node_id, visit, status, "
            "decision, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_key, task.task_number, "user_approval", 1,
                ExecutionStatus.COMPLETED.value, Decision.APPROVED.value,
                stamp_iso, stamp_iso,
            ),
        )
        svc._conn.execute(
            "INSERT INTO work_context_entries "
            "(task_project, task_number, entry_type, actor, created_at, text) "
            "VALUES (?, ?, 'plan_approved', 'system', ?, '{}')",
            (project_key, task.task_number, stamp_iso),
        )
        svc._conn.commit()
    finally:
        svc.close()


def _seed_queued_worker_task(
    project_path: Path,
    project_key: str,
    *,
    title: str = "Work item",
    link_to_plan: bool = True,
) -> str:
    """Create a queued worker-role task. When ``link_to_plan=True``, link
    it as a child of the approved plan_project task so the staleness
    check (#281) doesn't flag it as post-plan drift."""
    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    svc = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        task = svc.create(
            title=title,
            description="implementation",
            type="task",
            project=project_key,
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            priority="normal",
        )
        svc.queue(task.task_id, "test")
        if link_to_plan:
            plans = [
                t for t in svc.list_tasks(project=project_key)
                if t.flow_template_id == "plan_project"
                and t.work_status == WorkStatus.DONE
            ]
            if plans:
                svc.link(plans[0].task_id, task.task_id, "parent")
        return task.task_id
    finally:
        svc.close()


def _valid_work_output() -> WorkOutput:
    return WorkOutput(
        type=OutputType.CODE_CHANGE,
        summary="Implemented the change",
        artifacts=[
            Artifact(
                kind=ArtifactKind.COMMIT,
                description="implementation",
                ref="abc123",
            )
        ],
    )


def _move_task_to_rework(work: SQLiteWorkService, task_id: str) -> None:
    work.claim(task_id, "worker")
    work.node_done(task_id, "worker", _valid_work_output())
    work.reject(task_id, "reviewer", "needs rework")


def test_auto_claim_claims_next_queued_task_when_capacity_available(tmp_path: Path) -> None:
    """The Notesy regression: project has an approved plan + queued
    worker-role task + zero active workers → sweep auto-claims it."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    _write_plan(project_path)
    _seed_approved_plan(project_path, "proj")
    task_id = _seed_queued_worker_task(project_path, "proj")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        svc = _svc_defaults()
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )
        # Task should now be in_progress + reflected in totals.
        assert totals["by_outcome"].get("auto_claim_spawned", 0) == 1
        task = work.get(task_id)
        assert task.work_status == WorkStatus.IN_PROGRESS
    finally:
        work.close()


def test_auto_claim_respects_capacity_cap(tmp_path: Path) -> None:
    """Cap of 1 + already-active worker → no new claim."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    _write_plan(project_path)
    _seed_approved_plan(project_path, "proj")
    _seed_queued_worker_task(project_path, "proj", title="first")
    task_id_2 = _seed_queued_worker_task(project_path, "proj", title="second")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        # First claim fills the 1-slot cap.
        svc = _svc_defaults(max_concurrent_per_project=1)
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(svc, work, _FakeProject(key="proj", path=project_path), totals)
        assert totals["by_outcome"].get("auto_claim_spawned", 0) == 1

        # Second call: cap hit, no new spawn.
        totals2 = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(svc, work, _FakeProject(key="proj", path=project_path), totals2)
        assert totals2["by_outcome"].get("auto_claim_spawned", 0) == 0
        assert work.get(task_id_2).work_status == WorkStatus.QUEUED
    finally:
        work.close()


def test_auto_claim_counts_rework_against_capacity(tmp_path: Path) -> None:
    """#816: rejected work is still active worker load for capacity."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    _write_plan(project_path)
    _seed_approved_plan(project_path, "proj")
    rework_task_id = _seed_queued_worker_task(
        project_path, "proj", title="first",
    )
    queued_task_id = _seed_queued_worker_task(
        project_path, "proj", title="second",
    )

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        bus.clear_listeners()
        _move_task_to_rework(work, rework_task_id)
        assert work.get(rework_task_id).work_status == WorkStatus.REWORK

        svc = _svc_defaults(max_concurrent_per_project=1)
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )

        assert totals["by_outcome"].get("auto_claim_spawned", 0) == 0
        assert work.get(queued_task_id).work_status == WorkStatus.QUEUED
    finally:
        work.close()


def test_auto_claim_skips_when_plan_gate_closed(tmp_path: Path) -> None:
    """No approved plan → no auto-claim. Same bar as pm task claim."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    # No plan file, no plan-project approval.
    task_id = _seed_queued_worker_task(project_path, "proj")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        svc = _svc_defaults()
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(svc, work, _FakeProject(key="proj", path=project_path), totals)
        assert "auto_claim_spawned" not in totals["by_outcome"]
        assert work.get(task_id).work_status == WorkStatus.QUEUED
    finally:
        work.close()


def test_auto_claim_ignores_chat_feedback_when_checking_plan_staleness(tmp_path: Path) -> None:
    """A newer chat-flow rejection artifact must not freeze worker pickup."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    _write_plan(project_path)
    _seed_approved_plan(
        project_path,
        "proj",
        approved_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    task_id = _seed_queued_worker_task(project_path, "proj")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        work.create(
            title="Rejected proj/1 — Implement thing",
            description="Returned to rework.",
            type="task",
            project="proj",
            flow_template="chat",
            roles={"requester": "reviewer", "operator": "user"},
            priority="high",
            labels=["review_feedback", "task:proj/1", "project:proj"],
        )

        svc = _svc_defaults()
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )

        assert totals["by_outcome"].get("auto_claim_spawned", 0) == 1
        assert work.get(task_id).work_status == WorkStatus.IN_PROGRESS
    finally:
        work.close()


def test_dead_claim_check_rejects_sibling_project_windows(tmp_path: Path) -> None:
    """#807: a sibling project's window must not be accepted as proof
    that the current task's worker is alive.

    Project ``app`` task 7 has a stale claim and its real worker
    window is gone. A sibling project's window ``task-web_app-7``
    contains the substring ``app`` and ends with ``-7``, which the
    pre-fix check accepted as a live worker for ``app/7``. The fix
    compares against the exact ``task-<project>-<N>`` shape.
    """
    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        _tmux_window_alive_for_task,
    )

    sibling_window = SimpleNamespace(name="task-web_app-7", pane_dead=False)
    svc = _svc_defaults(
        session_service=_FakeSessionService(windows=[sibling_window]),
    )

    assert _tmux_window_alive_for_task(svc, "app", 7) is False


def test_dead_claim_check_accepts_exact_match(tmp_path: Path) -> None:
    """The check must still accept the exact window name ``task-<project>-<N>``."""
    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        _tmux_window_alive_for_task,
    )

    own_window = SimpleNamespace(name="task-app-7", pane_dead=False)
    svc = _svc_defaults(
        session_service=_FakeSessionService(windows=[own_window]),
    )

    assert _tmux_window_alive_for_task(svc, "app", 7) is True


def test_recover_dead_claims_returns_task_to_claimable_queue(tmp_path: Path) -> None:
    """#768 regression: a dead worker must leave the task genuinely
    claimable again, not bounced back to ``in_progress``.

    #806: recovery now preserves ``current_node_id`` and the active
    execution row (marked ``abandoned``) so the timeline keeps the
    history of attempts on the stranded node. The reclaim resumes on
    that same node and bumps the visit count rather than starting over
    from the flow's start node.
    """
    bus.clear_listeners()
    project_path = tmp_path / "proj"
    project_path.mkdir()
    task_id = _seed_queued_worker_task(
        project_path, "proj", link_to_plan=False,
    )

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        claimed = work.claim(task_id, "worker")
        original_node_id = claimed.current_node_id
        assert original_node_id is not None

        svc = _svc_defaults(session_service=_FakeSessionService())
        totals = {"considered": 0, "by_outcome": {}}
        _recover_dead_claims(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )

        assert totals["by_outcome"].get("auto_claim_recovered", 0) == 1
        recovered = work.get(task_id)
        assert recovered.work_status == WorkStatus.QUEUED
        assert recovered.assignee is None
        # #806: the node id and execution history must survive recovery.
        assert recovered.current_node_id == original_node_id
        statuses = [e.status for e in recovered.executions]
        assert ExecutionStatus.ABANDONED in statuses

        reclaimed = work.claim(task_id, "worker")
        assert reclaimed.work_status == WorkStatus.IN_PROGRESS
        # Reclaim stays on the stranded node — does not silently
        # restart at the flow start.
        assert reclaimed.current_node_id == original_node_id
        # Visit count incremented past the abandoned attempt.
        visits_after = [
            e.visit for e in reclaimed.executions
            if e.node_id == original_node_id
        ]
        assert max(visits_after) > 1
    finally:
        work.close()


def test_recover_dead_claims_returns_rework_task_to_claimable_queue(
    tmp_path: Path,
) -> None:
    """#816: a dead worker on a rejected task must not strand REWORK."""
    bus.clear_listeners()
    project_path = tmp_path / "proj"
    project_path.mkdir()
    task_id = _seed_queued_worker_task(
        project_path, "proj", link_to_plan=False,
    )

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        _move_task_to_rework(work, task_id)
        rework = work.get(task_id)
        assert rework.work_status == WorkStatus.REWORK
        original_node_id = rework.current_node_id
        assert original_node_id is not None

        svc = _svc_defaults(session_service=_FakeSessionService())
        totals = {"considered": 0, "by_outcome": {}}
        _recover_dead_claims(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )

        assert totals["by_outcome"].get("auto_claim_recovered", 0) == 1
        recovered = work.get(task_id)
        assert recovered.work_status == WorkStatus.QUEUED
        assert recovered.assignee is None
        assert recovered.current_node_id == original_node_id
        statuses = [e.status for e in recovered.executions]
        assert ExecutionStatus.ABANDONED in statuses

        reclaimed = work.claim(task_id, "worker")
        assert reclaimed.work_status == WorkStatus.IN_PROGRESS
        assert reclaimed.current_node_id == original_node_id
        visits_after = [
            e.visit for e in reclaimed.executions
            if e.node_id == original_node_id
        ]
        assert max(visits_after) > 2
    finally:
        work.close()
