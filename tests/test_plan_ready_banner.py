"""Dashboard plan-ready banner — Rule 3 of the #1633 cascade fix.

When a project has a ``plan_project`` task in ``done`` but the
canonical plan-review surface isn't firing, today's dashboard says
"no summary" — leaving the user staring at a project with a fully
formed plan and no affordance to read or approve it. The banner says
"the plan IS the summary" and points at the [a] hotkey.

Tests cover the predicate (fires only when conditions match) and the
render shape (advertises the downstream task count + the keybinding).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from pollypm.cockpit_sections.plan_ready_banner import (
    find_done_plan_project_task,
    maybe_render_plan_ready_banner,
    render_plan_ready_banner,
)


@dataclass
class _Status:
    value: str


@dataclass
class _FakeTask:
    project: str
    task_number: int
    flow_template_id: str = "plan_project"
    work_status: _Status = None  # set in __post_init__
    updated_at: datetime | None = None
    title: str = "Plan"

    def __post_init__(self) -> None:
        if self.work_status is None:
            self.work_status = _Status("done")

    @property
    def task_id(self) -> str:
        return f"{self.project}/{self.task_number}"


def _done_plan() -> _FakeTask:
    return _FakeTask(
        project="samblog",
        task_number=1,
        updated_at=datetime(2026, 5, 18, 9, 0, 0, tzinfo=timezone.utc),
    )


def _queued_impl(n: int) -> _FakeTask:
    return _FakeTask(
        project="samblog",
        task_number=n,
        flow_template_id="standard",
        work_status=_Status("queued"),
    )


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def test_finds_done_plan_project_task() -> None:
    tasks = [_done_plan(), _queued_impl(3)]
    plan = find_done_plan_project_task(tasks)
    assert plan is not None
    assert plan.task_id == "samblog/1"


def test_no_match_when_plan_still_in_review() -> None:
    plan = _FakeTask(
        project="samblog",
        task_number=1,
        work_status=_Status("review"),
    )
    assert find_done_plan_project_task([plan]) is None


def test_no_match_when_no_plan_project_task() -> None:
    impl = _queued_impl(5)
    assert find_done_plan_project_task([impl]) is None


def test_freshest_done_plan_wins_on_replan() -> None:
    older = _FakeTask(
        project="samblog",
        task_number=1,
        updated_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )
    newer = _FakeTask(
        project="samblog",
        task_number=2,
        updated_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    result = find_done_plan_project_task([older, newer])
    assert result.task_id == "samblog/2"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_advertises_downstream_count() -> None:
    plan = _done_plan()
    tasks = [plan, _queued_impl(3), _queued_impl(4), _queued_impl(5)]
    lines = render_plan_ready_banner(tasks=tasks, plan_task=plan)
    rendered = "\n".join(lines)
    assert "Plan ready" in rendered
    assert "samblog/1" in rendered
    assert "Plan is complete" in rendered
    assert "3 tasks ready to start" in rendered
    assert "[a]" in rendered  # the approve hotkey


def test_render_singular_when_one_downstream_task() -> None:
    plan = _done_plan()
    tasks = [plan, _queued_impl(3)]
    rendered = "\n".join(render_plan_ready_banner(tasks=tasks, plan_task=plan))
    assert "1 task ready to start" in rendered
    assert "1 tasks" not in rendered  # singular grammar


def test_render_omits_count_when_no_downstream() -> None:
    plan = _done_plan()
    rendered = "\n".join(render_plan_ready_banner(tasks=[plan], plan_task=plan))
    # No "0 tasks" leak — the count phrase simply doesn't render.
    assert "0 task" not in rendered
    # But the banner still surfaces the plan + hotkey.
    assert "Plan is complete" in rendered
    assert "[a]" in rendered


# ---------------------------------------------------------------------------
# maybe_render orchestration
# ---------------------------------------------------------------------------


def test_maybe_render_skips_when_full_surface_active() -> None:
    """When the plan-review surface IS firing, the banner stays quiet."""
    plan = _done_plan()
    lines = maybe_render_plan_ready_banner(
        tasks=[plan], plan_review_surface_active=True,
    )
    assert lines == []


def test_maybe_render_fires_for_samblog_repro() -> None:
    """The exact samblog/1 shape — done plan, no surface active."""
    plan = _done_plan()
    impls = [_queued_impl(3), _queued_impl(4)]
    lines = maybe_render_plan_ready_banner(
        tasks=[plan, *impls], plan_review_surface_active=False,
    )
    assert lines  # non-empty
    rendered = "\n".join(lines)
    assert "Plan ready" in rendered
    assert "samblog/1" in rendered


def test_maybe_render_skips_when_no_done_plan() -> None:
    """A project without a done plan_project task gets nothing."""
    assert maybe_render_plan_ready_banner(
        tasks=[], plan_review_surface_active=False,
    ) == []
    only_impl = _queued_impl(3)
    assert maybe_render_plan_ready_banner(
        tasks=[only_impl], plan_review_surface_active=False,
    ) == []
