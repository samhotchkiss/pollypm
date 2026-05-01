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
    _open_workspace_project_work_service,
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


def test_open_workspace_project_work_service_wires_project_session_manager(
    tmp_path: Path, monkeypatch,
) -> None:
    """Workspace DB claims must still provision workers in the target project."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    workspace_db = workspace_root / ".pollypm" / "state.db"
    workspace_db.parent.mkdir(parents=True, exist_ok=True)
    seed = SQLiteWorkService(db_path=workspace_db, project_path=workspace_root)
    seed.close()

    project_path = tmp_path / "proj"
    project_path.mkdir()
    (project_path / ".git").mkdir()
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
        project_root=workspace_root,
        session_service=object(),
        config=SimpleNamespace(project=SimpleNamespace(tmux_session="pollypm")),
        storage_closet_name="pollypm-storage-closet",
    )
    project = _FakeProject(key="proj", path=project_path)

    svc = _open_workspace_project_work_service(project, services)
    try:
        assert svc is not None
        assert type(getattr(svc, "_session_mgr", None)).__name__ == "DummySessionManager"
        assert wired["kwargs"]["project_path"] == project_path
        assert wired["kwargs"]["storage_closet_name"] == "pollypm-storage-closet"
        assert wired["kwargs"]["session_service"] is services.session_service
    finally:
        if svc is not None:
            svc.close()


def test_sweep_runs_auto_claim_against_workspace_root_db(monkeypatch, tmp_path: Path) -> None:
    """Workspace-root tasks need the same auto-claim pass as per-project DBs."""
    from pollypm.plugins_builtin.task_assignment_notify.handlers import sweep as sweep_mod

    class WorkspaceWork:
        def __init__(self) -> None:
            self.closed = False

        def list_tasks(self, *args, **kwargs) -> list[object]:
            return []

        def close(self) -> None:
            self.closed = True

    sweep_work = WorkspaceWork()
    claim_work = WorkspaceWork()
    project = _FakeProject(key="proj", path=tmp_path / "proj")
    calls: list[tuple[object, object]] = []
    services = _svc_defaults(
        work_service=sweep_work,
        known_projects=(project,),
        auto_claim=True,
    )

    monkeypatch.setattr(
        sweep_mod,
        "load_runtime_services",
        lambda config_path=None: services,
    )
    monkeypatch.setattr(
        sweep_mod,
        "_open_workspace_project_work_service",
        lambda claim_project, _services: (
            claim_work if claim_project is project else None
        ),
    )
    monkeypatch.setattr(
        sweep_mod,
        "_recover_dead_claims",
        lambda _services, _work, _project, _totals: None,
    )
    monkeypatch.setattr(
        sweep_mod,
        "_auto_claim_next",
        lambda _services, claim_work, claim_project, _totals, **_kw: calls.append(
            (claim_work, claim_project)
        ),
    )

    result = sweep_mod.task_assignment_sweep_handler({})

    assert result["outcome"] == "swept"
    assert calls == [(claim_work, project)]
    assert sweep_work.closed is True
    assert claim_work.closed is True


def test_workspace_plan_missing_survives_missing_project_db(
    monkeypatch, tmp_path: Path,
) -> None:
    """Workspace-root plan gates must not be cleared by the per-project pass."""
    from pollypm.plugins_builtin.task_assignment_notify.handlers import sweep as sweep_mod

    class WorkspaceWork:
        def __init__(self) -> None:
            self.closed = False

        def list_tasks(self, *args, **kwargs) -> list[object]:
            return []

        def close(self) -> None:
            self.closed = True

    sweep_work = WorkspaceWork()
    claim_work = WorkspaceWork()
    project = _FakeProject(key="proj", path=tmp_path / "proj")
    services = _svc_defaults(
        work_service=sweep_work,
        known_projects=(project,),
        auto_claim=True,
    )
    cleared: list[str] = []

    def fake_auto_claim(
        _services,
        _work,
        claim_project,
        _totals,
        *,
        plan_missing_projects=None,
    ) -> None:
        assert claim_project is project
        assert plan_missing_projects is not None
        plan_missing_projects.add("proj")

    monkeypatch.setattr(
        sweep_mod,
        "load_runtime_services",
        lambda config_path=None: services,
    )
    monkeypatch.setattr(
        sweep_mod,
        "_open_workspace_project_work_service",
        lambda claim_project, _services: (
            claim_work if claim_project is project else None
        ),
    )
    monkeypatch.setattr(
        sweep_mod,
        "_open_project_work_service",
        lambda _project, _services: None,
    )
    monkeypatch.setattr(
        sweep_mod,
        "_recover_dead_claims",
        lambda _services, _work, _project, _totals: None,
    )
    monkeypatch.setattr(sweep_mod, "_auto_claim_next", fake_auto_claim)
    monkeypatch.setattr(
        sweep_mod,
        "_clear_plan_missing_alert",
        lambda _services, project: cleared.append(project),
    )

    result = sweep_mod.task_assignment_sweep_handler({})

    assert result["plan_missing_alerts"] == 1
    assert cleared == []
    assert sweep_work.closed is True
    assert claim_work.closed is True


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
    labels: list[str] | None = None,
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
            labels=labels,
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


def test_auto_claim_honors_bypass_plan_gate_label(tmp_path: Path) -> None:
    """Explicit operator bypasses must not be filtered out before claim."""
    project_path = tmp_path / "proj"
    project_path.mkdir()
    task_id = _seed_queued_worker_task(
        project_path,
        "proj",
        link_to_plan=False,
        labels=["bypass_plan_gate"],
    )

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        svc = _svc_defaults()
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )
        assert totals["by_outcome"].get("auto_claim_spawned", 0) == 1
        assert totals["by_outcome"].get("auto_claim_skipped_plan_missing", 0) == 0
        assert work.get(task_id).work_status == WorkStatus.IN_PROGRESS
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
        plan_missing_projects: set[str] = set()
        _auto_claim_next(
            svc,
            work,
            _FakeProject(key="proj", path=project_path),
            totals,
            plan_missing_projects=plan_missing_projects,
        )
        assert "auto_claim_spawned" not in totals["by_outcome"]
        assert plan_missing_projects == {"proj"}
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


class _RecordingStore:
    """Tiny alert/event store double for circuit-breaker tests (#1012)."""

    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str, str]] = []
        self.events: list[dict[str, object]] = []

    def upsert_alert(
        self, scope: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.alerts.append((scope, alert_type, severity, message))

    def append_event(
        self,
        *,
        scope: str,
        sender: str,
        subject: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.events.append(
            {
                "scope": scope,
                "sender": sender,
                "subject": subject,
                "payload": payload or {},
            }
        )


def test_recover_dead_claims_circuit_breaker_after_consecutive_abandonments(
    tmp_path: Path,
) -> None:
    """#1012: after MAX_CONSECUTIVE_ABANDONMENTS abandoned executions on
    the active node, the recovery sweep must stop releasing the stale
    claim and escalate to a ``spawn_failed_persistent`` error alert.

    Pre-fix the loop was unbounded: ``release_stale_claim`` → re-queue
    → auto-claim → ``provision_worker`` short-circuited on the orphan
    row → no tmux window → next tick repeats. ``bikepath/8`` racked up
    60+ ``build vN abandoned`` rows in ~7h.
    """
    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        MAX_CONSECUTIVE_ABANDONMENTS,
        SPAWN_FAILED_PERSISTENT_ALERT_TYPE,
    )

    bus.clear_listeners()
    project_path = tmp_path / "proj"
    project_path.mkdir()
    task_id = _seed_queued_worker_task(
        project_path, "proj", link_to_plan=False,
    )

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        # Replay MAX_CONSECUTIVE_ABANDONMENTS spawn-fail cycles by
        # hand: each iteration claims (creates an active execution),
        # then release_stale_claim marks it abandoned + returns the
        # task to queued. After enough cycles the streak hits the cap.
        store = _RecordingStore()
        svc = _svc_defaults(
            session_service=_FakeSessionService(),
            state_store=store,
            msg_store=store,
        )
        totals = {"considered": 0, "by_outcome": {}}

        for _ in range(MAX_CONSECUTIVE_ABANDONMENTS):
            work.claim(task_id, "worker")
            _recover_dead_claims(
                svc, work, _FakeProject(key="proj", path=project_path), totals,
            )

        assert totals["by_outcome"].get("auto_claim_recovered", 0) == (
            MAX_CONSECUTIVE_ABANDONMENTS
        )

        # One more claim attempt — this is the cycle that should trip
        # the circuit breaker.
        work.claim(task_id, "worker")
        _recover_dead_claims(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )

        # Circuit breaker tripped: no fresh release this tick.
        assert totals["by_outcome"].get("auto_claim_circuit_breaker", 0) == 1
        assert totals["by_outcome"].get("auto_claim_recovered", 0) == (
            MAX_CONSECUTIVE_ABANDONMENTS
        )

        # The task stays in_progress with the dead claim — we deliberately
        # do NOT re-queue it on a tripped breaker.
        latest = work.get(task_id)
        assert latest.work_status == WorkStatus.IN_PROGRESS

        # An error-severity ``spawn_failed_persistent`` alert was raised
        # against the canonical worker-session candidate.
        spawn_failed = [
            entry for entry in store.alerts
            if entry[1] == SPAWN_FAILED_PERSISTENT_ALERT_TYPE
        ]
        assert spawn_failed, "expected spawn_failed_persistent alert"
        scope, _alert_type, severity, message = spawn_failed[-1]
        assert severity == "error"
        assert task_id in message
        # Alert is keyed by the canonical worker-session candidate.
        assert scope.startswith("worker") and "proj" in scope
    finally:
        work.close()


def test_recover_dead_claims_streak_counts_only_active_node(tmp_path: Path) -> None:
    """#1012: a successful turn between failures resets the streak.

    The breaker must only react to the *current* node's recent
    abandonments. A reject-bounce that walks the task back through the
    review node before re-entering build still counts each visit at
    the build node, but a completed build between two failures should
    reset the streak — that's a working worker, not a broken spawn.
    """
    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        _consecutive_abandonments_at_active_node,
    )

    # Hand-rolled task double — we just need ``current_node_id`` and
    # ``executions`` shaped like ``FlowNodeExecution`` rows.
    task = SimpleNamespace(
        current_node_id="build",
        executions=[
            SimpleNamespace(node_id="build", visit=1, status=ExecutionStatus.ABANDONED),
            # A completed visit must reset the streak — the worker did
            # real work between failures, so the spawn pipeline isn't
            # persistently broken.
            SimpleNamespace(node_id="build", visit=2, status=ExecutionStatus.COMPLETED),
            SimpleNamespace(node_id="build", visit=3, status=ExecutionStatus.ABANDONED),
            SimpleNamespace(node_id="build", visit=4, status=ExecutionStatus.ABANDONED),
        ],
    )

    assert _consecutive_abandonments_at_active_node(task) == 2


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


# ---------------------------------------------------------------------------
# #936 — per-task worker auto-spawn regression tests
# ---------------------------------------------------------------------------


def test_auto_claim_spawns_per_task_worker_when_no_live_session(tmp_path: Path) -> None:
    """#936 regression: queued worker-role task with no live session must
    auto-claim AND invoke the session manager's per-task ``provision_worker``.

    The bug from #936 was that the cycle stalled at ``queued`` because
    nothing called ``pm task claim`` automatically. The auto-claim sweep
    must (a) flip the DB to ``in_progress`` and (b) call the session
    manager so the per-task tmux window actually spawns. This test
    asserts both halves so a future regression that drops the spawn
    side (claim-only) is caught.
    """
    bus.clear_listeners()
    project_path = tmp_path / "proj"
    project_path.mkdir()
    _write_plan(project_path)
    _seed_approved_plan(project_path, "proj")
    task_id = _seed_queued_worker_task(project_path, "proj")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)

    provisions: list[tuple[str, str]] = []

    class FakeSessionManager:
        def provision_worker(self, task_id: str, agent_name: str):
            provisions.append((task_id, agent_name))
            return None

        def teardown_worker(self, task_id: str) -> None:
            return None

    work.set_session_manager(FakeSessionManager())

    try:
        svc = _svc_defaults(session_service=_FakeSessionService())
        totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), totals,
        )

        assert totals["by_outcome"].get("auto_claim_spawned", 0) == 1
        task = work.get(task_id)
        assert task.work_status == WorkStatus.IN_PROGRESS
        # Per-task spawn side: provision_worker must have been invoked
        # with the queued task id. This is what #919 wired in and #936
        # depends on for the per-task pane to come up.
        assert provisions == [(task_id, "worker")]
    finally:
        work.close()


def test_auto_claim_skips_plan_missing_task(tmp_path: Path) -> None:
    """#936 mandate: plan-gated tasks must NOT auto-claim.

    A worker-role task in a project with no approved plan should stay
    queued and emit a ``plan_missing`` alert, not silently get claimed.
    Mirrors :func:`test_auto_claim_skips_when_plan_gate_closed` but
    pinned with the #936 framing so the contract stays explicit.
    """
    project_path = tmp_path / "proj"
    project_path.mkdir()
    # No plan file, no approved plan_project task — plan gate is closed.
    task_id = _seed_queued_worker_task(project_path, "proj")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        svc = _svc_defaults()
        totals = {"considered": 0, "by_outcome": {}}
        plan_missing: set[str] = set()
        _auto_claim_next(
            svc,
            work,
            _FakeProject(key="proj", path=project_path),
            totals,
            plan_missing_projects=plan_missing,
        )

        assert "auto_claim_spawned" not in totals["by_outcome"]
        assert (
            totals["by_outcome"].get("auto_claim_skipped_plan_missing", 0) == 1
        )
        assert plan_missing == {"proj"}
        assert work.get(task_id).work_status == WorkStatus.QUEUED
    finally:
        work.close()


def test_auto_claim_idempotent_under_concurrent_sweeps(tmp_path: Path) -> None:
    """#936 mandate: concurrent sweeps must not double-claim a task.

    A single queued worker-role task. Two back-to-back ``_auto_claim_next``
    calls (simulating a heartbeat sweep racing with a manual ``pm
    heartbeat`` invocation): the first claims, the second sees the
    task in ``in_progress`` and records ``auto_claim_failed`` (or
    silently no-ops because there are no longer any queued candidates).
    Either way the task stays in ``in_progress`` exactly once.
    """
    bus.clear_listeners()
    project_path = tmp_path / "proj"
    project_path.mkdir()
    _write_plan(project_path)
    _seed_approved_plan(project_path, "proj")
    task_id = _seed_queued_worker_task(project_path, "proj")

    db_path = project_path / ".pollypm" / "state.db"
    work = SQLiteWorkService(db_path=db_path, project_path=project_path)
    try:
        svc = _svc_defaults()

        first_totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), first_totals,
        )
        assert first_totals["by_outcome"].get("auto_claim_spawned", 0) == 1
        assert work.get(task_id).work_status == WorkStatus.IN_PROGRESS

        second_totals = {"considered": 0, "by_outcome": {}}
        _auto_claim_next(
            svc, work, _FakeProject(key="proj", path=project_path), second_totals,
        )
        # Second sweep must not produce another spawn for the same task.
        assert second_totals["by_outcome"].get("auto_claim_spawned", 0) == 0
        # Capacity check sees the now-in_progress task as active load,
        # so no new candidate is even considered (the helper returns
        # before reaching ``work.claim``). ``auto_claim_failed`` should
        # therefore stay zero — failures are reserved for genuine
        # claim-call exceptions, not "nothing to do".
        assert "auto_claim_failed" not in second_totals["by_outcome"]
        assert work.get(task_id).work_status == WorkStatus.IN_PROGRESS
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
