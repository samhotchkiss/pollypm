from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pollypm.cockpit_sections.dashboard import _build_dashboard
from pollypm.cockpit_sections.health import (
    format_project_health_scorecard,
    project_health_glyph,
    project_health_rank,
    stuck_task_count,
)
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
        title: str,
        status: str,
        updated_at: datetime,
        transitions: list[_Transition] | None = None,
        assignee: str | None = None,
        current_node_id: str | None = None,
    ) -> None:
        self.title = title
        self.work_status = WorkStatus(status)
        self.updated_at = updated_at
        self.transitions = transitions or []
        self.assignee = assignee
        self.current_node_id = current_node_id


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


def test_format_project_health_scorecard_includes_counts_cycle_and_health() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    done = _Task(
        title="Ship launch flow",
        status="done",
        updated_at=now - timedelta(hours=1),
        transitions=[
            _Transition("in_progress", "worker", now - timedelta(hours=2, minutes=15)),
            _Transition("done", "reviewer", now - timedelta(hours=1, minutes=30)),
        ],
    )
    active = _Task(
        title="Polish account UI",
        status="in_progress",
        updated_at=now - timedelta(minutes=20),
    )
    line = format_project_health_scorecard(
        "web-app",
        {"in_progress": 1, "review": 0, "blocked": 0},
        [done, active],
        now=now,
    )
    assert line == "web-app · 1 in progress · 0 review · 0 blocked · 45m cycle · 🟢"


def test_health_thresholds_track_stuck_tasks() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    fresh = _Task(
        title="Fresh",
        status="in_progress",
        updated_at=now - timedelta(minutes=10),
    )
    stale = _Task(
        title="Stale",
        status="review",
        updated_at=now - timedelta(hours=7),
    )
    very_stale = _Task(
        title="Very stale",
        status="blocked",
        updated_at=now - timedelta(hours=10),
    )
    assert stuck_task_count([fresh, stale], now=now) == 1
    assert project_health_glyph([fresh, stale], now=now) == "🟡"
    assert stuck_task_count([stale, very_stale, stale], now=now) == 3
    assert project_health_glyph([stale, very_stale, stale], now=now) == "🔴"
    assert project_health_rank([fresh], now=now) == 2


def test_global_dashboard_surfaces_project_scorecards_sorted_by_health(monkeypatch) -> None:
    now = datetime.now(UTC)
    healthy_tasks = [
        _Task(
            title="Healthy",
            status="in_progress",
            updated_at=now - timedelta(minutes=15),
        )
    ]
    stuck_tasks = [
        _Task(
            title=f"Stuck {index}",
            status="review",
            updated_at=now - timedelta(hours=7 + index),
        )
        for index in range(3)
    ]

    project_data = {
        "red": (
            {"in_progress": [], "review": stuck_tasks, "queued": [], "blocked": [], "done": []},
            {"review": 3, "blocked": 0, "in_progress": 0},
        ),
        "green": (
            {"in_progress": healthy_tasks, "review": [], "queued": [], "blocked": [], "done": []},
            {"review": 0, "blocked": 0, "in_progress": 1},
        ),
    }

    monkeypatch.setattr(
        "pollypm.cockpit_sections.dashboard._dashboard_project_tasks",
        lambda project_key, _path: project_data[project_key],
    )

    out = _build_dashboard(
        _Supervisor(),
        _Config(
            {
                "red": _Project("red", "api"),
                "green": _Project("green", "notesy"),
            }
        ),
    )

    assert "Projects" in out
    red_line = "api · 0 in progress · 3 review · 0 blocked · — cycle · 🔴"
    green_line = "notesy · 1 in progress · 0 review · 0 blocked · — cycle · 🟢"
    assert red_line in out
    assert green_line in out
    assert out.index(red_line) < out.index(green_line)
