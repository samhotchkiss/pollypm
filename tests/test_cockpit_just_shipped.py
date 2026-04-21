from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.cockpit_sections.dashboard import _build_dashboard
from pollypm.cockpit_sections.just_shipped import _section_just_shipped
from pollypm.work.models import WorkStatus


@dataclass
class _Transition:
    to_state: str
    actor: str
    timestamp: datetime


class _Task:
    def __init__(
        self,
        *,
        task_number: int,
        title: str,
        updated_at: datetime,
        transitions: list[_Transition],
    ) -> None:
        self.task_number = task_number
        self.title = title
        self.work_status = WorkStatus.DONE
        self.updated_at = updated_at
        self.transitions = transitions


class _Project:
    def __init__(self, key: str, name: str) -> None:
        self.key = key
        self.name = name
        self.path = Path("/tmp") / key

    def display_label(self) -> str:
        return self.name


class _Store:
    def open_alerts(self):
        return []

    def recent_events(self, limit=300):
        return []


class _Supervisor:
    store = _Store()


class _Config:
    def __init__(self, projects: dict[str, _Project]) -> None:
        self.projects = projects


def _done_task(task_number: int, *, now: datetime, age_hours: int, cycle_minutes: int) -> _Task:
    end = now - timedelta(hours=age_hours)
    start = end - timedelta(minutes=cycle_minutes)
    return _Task(
        task_number=task_number,
        title=f"Task {task_number}",
        updated_at=end,
        transitions=[
            _Transition("in_progress", "worker", start),
            _Transition("done", "russell", end),
        ],
    )


def test_section_just_shipped_shows_recent_approvals_only() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    recent = _done_task(1, now=now, age_hours=2, cycle_minutes=45)
    older = _done_task(2, now=now, age_hours=30, cycle_minutes=20)
    lines = _section_just_shipped(
        [("notesy", recent), ("notesy", older)],
        now=now,
    )
    joined = "\n".join(lines)
    assert "🎉 Just shipped" in joined
    assert "notesy/1  45m cycle" in joined
    assert "2h ago" in joined
    assert "notesy/2" not in joined


def test_global_dashboard_includes_just_shipped_panel(monkeypatch) -> None:
    now = datetime.now(UTC)
    shipped = _done_task(3, now=now, age_hours=4, cycle_minutes=20)
    project_data = {
        "notesy": (
            {"in_progress": [], "review": [], "queued": [], "blocked": [], "done": [shipped]},
            {"done": 1},
        )
    }

    monkeypatch.setattr(
        "pollypm.cockpit_sections.dashboard._dashboard_project_tasks",
        lambda project_key, _path: project_data[project_key],
    )

    out = _build_dashboard(
        _Supervisor(),
        _Config({"notesy": _Project("notesy", "Notesy")}),
    )

    assert "🎉 Just shipped" in out
    assert "notesy/3  20m cycle" in out
