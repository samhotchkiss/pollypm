from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pollypm.audit.log import EVENT_TASK_RECLAIMED, read_events
from pollypm.plugins_builtin.task_assignment_notify.handlers import sweep as sweep_mod
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    _auto_claim_next,
    _recover_dead_claims,
)
from pollypm.work.models import WorkStatus


class _FakeTmux:
    def list_windows(self, _session_name: str) -> list[object]:
        return []


class _FakeSessionService:
    tmux = _FakeTmux()

    def storage_closet_session_name(self) -> str:
        return "pollypm-storage-closet"


class _FakeWork:
    def __init__(self, task: SimpleNamespace) -> None:
        self.task = task
        self.released: list[tuple[str, str, str]] = []

    def list_tasks(self, *, project: str, work_status: str) -> list[SimpleNamespace]:
        if project == self.task.project and work_status == WorkStatus.IN_PROGRESS.value:
            return [self.task]
        return []

    def release_stale_claim(self, task_id: str, actor: str, *, reason: str) -> None:
        self.released.append((task_id, actor, reason))


class _FakeAutoClaimWork:
    def __init__(
        self,
        queued: list[SimpleNamespace],
        *,
        claim_error: Exception | None = None,
    ) -> None:
        self.queued = queued
        self.claim_error = claim_error
        self.claimed: list[tuple[str, str]] = []

    def list_tasks(
        self, *, project: str, work_status: str,
    ) -> list[SimpleNamespace]:
        if work_status == WorkStatus.QUEUED.value:
            return [task for task in self.queued if task.project == project]
        return []

    def claim(self, task_id: str, actor: str) -> None:
        if self.claim_error is not None:
            raise self.claim_error
        self.claimed.append((task_id, actor))


class _FakeMsgStore:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.alerts: list[tuple[str, str, str, str]] = []

    def append_event(self, **kwargs: object) -> None:
        self.events.append(kwargs)

    def upsert_alert(
        self, scope: str, alert_type: str, severity: str, message: str,
    ) -> None:
        self.alerts.append((scope, alert_type, severity, message))


def test_recover_dead_claims_emits_task_reclaimed_audit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit-home"))
    project_path = tmp_path / "demo"
    project_path.mkdir()
    task = SimpleNamespace(
        project="demo",
        task_number=7,
        task_id="demo/7",
        roles={"worker": "claude"},
        current_node_id="build",
        executions=[],
    )
    work = _FakeWork(task)
    services = SimpleNamespace(
        session_service=_FakeSessionService(),
        msg_store=SimpleNamespace(append_event=lambda **_kwargs: None),
        project_root=tmp_path,
    )
    project = SimpleNamespace(key="demo", path=project_path)
    totals = {"by_outcome": {}}

    _recover_dead_claims(services, work, project, totals)

    assert work.released == [
        ("demo/7", "auto_claim_sweep", "worker session missing"),
    ]
    assert totals["by_outcome"]["auto_claim_recovered"] == 1

    events = read_events("demo", project_path=project_path, event=EVENT_TASK_RECLAIMED)
    assert len(events) == 1
    event = events[0]
    assert event.subject == "demo/7"
    assert event.actor == "auto_claim_sweep"
    assert event.status == "ok"
    assert event.metadata["target_task"] == "demo/7"
    assert event.metadata["target_session"] == "task-demo-7"
    assert event.metadata["reason"] == "worker session missing; stale claim released"


def test_auto_claim_next_claims_worker_task_and_emits_audit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit-home"))
    monkeypatch.setattr(sweep_mod, "has_acceptable_plan", lambda *_a, **_kw: True)
    project_path = tmp_path / "demo"
    (project_path / ".pollypm").mkdir(parents=True)
    task = SimpleNamespace(
        project="demo",
        task_number=3,
        task_id="demo/3",
        roles={"worker": "worker"},
        labels=[],
        flow_template_id="standard",
    )
    work = _FakeAutoClaimWork([task])
    msg_store = _FakeMsgStore()
    services = SimpleNamespace(
        enforce_plan=True,
        plan_dir="docs/plan",
        max_concurrent_per_project=2,
        msg_store=msg_store,
        project_root=tmp_path,
    )
    project = SimpleNamespace(key="demo", path=project_path)
    totals = {"by_outcome": {}}

    _auto_claim_next(services, work, project, totals)

    assert work.claimed == [("demo/3", "auto_claim_sweep")]
    assert totals["by_outcome"]["auto_claim_spawned"] == 1
    assert msg_store.events[0]["subject"] == "worker_auto_claimed"

    events = read_events(
        "demo",
        project_path=project_path,
        event="auto_claim_spawned",
    )
    assert len(events) == 1
    event = events[0]
    assert event.subject == "demo/3"
    assert event.actor == "auto_claim_sweep"
    assert event.status == "ok"
    assert event.metadata["task_id"] == "demo/3"
    assert event.metadata["active_workers_before"] == 0
    assert event.metadata["cap"] == 2


def test_auto_claim_next_plan_missing_emits_skip_audit_without_claiming(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit-home"))
    monkeypatch.setattr(sweep_mod, "has_acceptable_plan", lambda *_a, **_kw: False)
    project_path = tmp_path / "demo"
    (project_path / ".pollypm").mkdir(parents=True)
    task = SimpleNamespace(
        project="demo",
        task_number=4,
        task_id="demo/4",
        roles={"worker": "worker"},
        labels=[],
        flow_template_id="standard",
    )
    work = _FakeAutoClaimWork([task])
    msg_store = _FakeMsgStore()
    services = SimpleNamespace(
        enforce_plan=True,
        plan_dir="docs/plan",
        max_concurrent_per_project=2,
        msg_store=msg_store,
        project_root=tmp_path,
    )
    project = SimpleNamespace(key="demo", path=project_path)
    totals = {"by_outcome": {}}
    plan_missing_projects: set[str] = set()

    _auto_claim_next(
        services,
        work,
        project,
        totals,
        plan_missing_projects=plan_missing_projects,
    )

    assert work.claimed == []
    assert totals["by_outcome"]["auto_claim_skipped_plan_missing"] == 1
    assert plan_missing_projects == {"demo"}
    assert msg_store.alerts

    _auto_claim_next(
        services,
        work,
        project,
        totals,
        plan_missing_projects=plan_missing_projects,
    )

    assert work.claimed == []
    assert totals["by_outcome"]["auto_claim_skipped_plan_missing"] == 2

    events = read_events(
        "demo",
        project_path=project_path,
        event="auto_claim_skipped_plan_missing",
    )
    assert len(events) == 2
    event = events[0]
    assert event.subject == "demo/4"
    assert event.actor == "auto_claim_sweep"
    assert event.status == "warn"
    assert event.metadata["task_id"] == "demo/4"
    assert event.metadata["reason"] == "acceptable plan missing"


def test_auto_claim_next_claim_failure_emits_failed_audit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(tmp_path / "audit-home"))
    monkeypatch.setattr(sweep_mod, "has_acceptable_plan", lambda *_a, **_kw: True)
    project_path = tmp_path / "demo"
    (project_path / ".pollypm").mkdir(parents=True)
    task = SimpleNamespace(
        project="demo",
        task_number=5,
        task_id="demo/5",
        roles={"worker": "worker"},
        labels=[],
        flow_template_id="standard",
    )
    work = _FakeAutoClaimWork([task], claim_error=RuntimeError("claim boom"))
    services = SimpleNamespace(
        enforce_plan=True,
        plan_dir="docs/plan",
        max_concurrent_per_project=2,
        msg_store=_FakeMsgStore(),
        project_root=tmp_path,
    )
    project = SimpleNamespace(key="demo", path=project_path)
    totals = {"by_outcome": {}}

    _auto_claim_next(services, work, project, totals)

    assert work.claimed == []
    assert totals["by_outcome"]["auto_claim_failed"] == 1

    events = read_events(
        "demo",
        project_path=project_path,
        event="auto_claim_failed",
    )
    assert len(events) == 1
    event = events[0]
    assert event.subject == "demo/5"
    assert event.actor == "auto_claim_sweep"
    assert event.status == "error"
    assert event.metadata["task_id"] == "demo/5"
    assert event.metadata["reason"] == "claim boom"
