from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pollypm.plugins_builtin.core_recurring import blocked_chain
from pollypm.work.models import WorkStatus


class _FakeWork:
    def __init__(
        self,
        task: SimpleNamespace,
        *,
        tasks_by_id: dict[str, SimpleNamespace] | None = None,
    ) -> None:
        self.task = task
        self.tasks_by_id = dict(tasks_by_id or {})

    def list_tasks(self, *, work_status: str) -> list[SimpleNamespace]:
        if work_status == WorkStatus.BLOCKED.value:
            return [self.task]
        return []

    def get(self, task_id: str) -> SimpleNamespace:
        return self.tasks_by_id[task_id]


class _FakeStore:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str, str]] = []

    def upsert_alert(
        self,
        session_name: str,
        alert_type: str,
        severity: str,
        message: str,
    ) -> None:
        self.alerts.append((session_name, alert_type, severity, message))


def test_blocked_task_with_no_blocker_rows_alerts(monkeypatch) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    task = SimpleNamespace(
        project="demo",
        task_number=7,
        task_id="demo/7",
    )
    monkeypatch.setattr(
        blocked_chain,
        "_blocked_since",
        lambda *_args, **_kwargs: now - timedelta(hours=2),
    )
    monkeypatch.setattr(
        blocked_chain,
        "_walk_blocker_chain",
        lambda *_args, **_kwargs: (set(), {}),
    )
    store = _FakeStore()

    counters = blocked_chain.sweep_blocked_chains(
        work=_FakeWork(task),
        msg_store=store,
        state_store=None,
        now=now,
        stale_threshold_seconds=3600,
    )

    assert counters["blocked_considered"] == 1
    assert counters["dead_end_detected"] == 1
    assert counters["alerts_raised"] == 1
    assert counters["skipped_no_blockers"] == 0
    assert store.alerts == [
        (
            "blocked-demo-7",
            blocked_chain.BLOCKED_DEAD_END_ALERT_TYPE,
            "warn",
            store.alerts[0][3],
        ),
    ]
    assert "has no blocker dependency rows" in store.alerts[0][3]


def test_cancelled_blocker_chain_alerts_to_project_scope(monkeypatch) -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    task = SimpleNamespace(
        project="demo",
        task_number=30,
        task_id="demo/30",
    )
    cancelled = SimpleNamespace(
        project="demo",
        task_number=26,
        task_id="demo/26",
        title="Needs your inputs",
    )
    monkeypatch.setattr(
        blocked_chain,
        "_blocked_since",
        lambda *_args, **_kwargs: now - timedelta(hours=2),
    )
    monkeypatch.setattr(
        blocked_chain,
        "_walk_blocker_chain",
        lambda *_args, **_kwargs: (
            {("demo", 26)},
            {("demo", 26): WorkStatus.CANCELLED.value},
        ),
    )
    store = _FakeStore()

    counters = blocked_chain.sweep_blocked_chains(
        work=_FakeWork(task, tasks_by_id={"demo/26": cancelled}),
        msg_store=store,
        state_store=None,
        now=now,
        stale_threshold_seconds=3600,
    )

    assert counters["blocked_considered"] == 1
    assert counters["dead_end_detected"] == 1
    assert counters["cancelled_blocker_detected"] == 1
    assert counters["alerts_raised"] == 1
    assert counters["cancelled_blocker_alerts_raised"] == 1
    assert store.alerts == [
        (
            "demo",
            "blocked_cancelled_blocker:demo/30",
            "warn",
            store.alerts[0][3],
        ),
    ]
    assert "demo/26 (Needs your inputs)" in store.alerts[0][3]
    assert "do not auto-satisfy dependencies" in store.alerts[0][3]
