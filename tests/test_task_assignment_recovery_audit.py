from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pollypm.audit.log import EVENT_TASK_RECLAIMED, read_events
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
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
