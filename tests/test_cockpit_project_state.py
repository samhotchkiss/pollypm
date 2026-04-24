from __future__ import annotations

from types import SimpleNamespace

from pollypm.cli_features.alerts import is_surfaceable_operational_alert
from pollypm.cockpit_project_state import (
    ProjectRailState,
    actionable_alert_task_ids,
    rollup_project_state,
)


def _task(
    number: int,
    status: str,
    *,
    node: str = "",
    flow: str = "implementation",
    project: str = "demo",
):
    return SimpleNamespace(
        project=project,
        task_number=number,
        task_id=f"{project}/{number}",
        work_status=status,
        current_node_id=node,
        flow_template_id=flow,
        labels=[],
    )


def test_rollup_red_when_all_nonterminal_tasks_wait_on_user() -> None:
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "blocked"),
            _task(2, "waiting_on_user"),
            _task(3, "done"),
        ],
    )

    assert rollup.state is ProjectRailState.RED
    assert rollup.badge == "🔴"
    assert rollup.sort_rank == 0
    assert rollup.actionable_key == "project:demo:issues"


def test_rollup_yellow_when_user_wait_is_mixed_with_automated_work() -> None:
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "blocked"),
            _task(2, "in_progress"),
        ],
    )

    assert rollup.state is ProjectRailState.YELLOW
    assert rollup.badge == "🟡"


def test_rollup_green_when_only_user_review_remains() -> None:
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "review", node="user_approval"),
            _task(2, "user-review"),
            _task(3, "done"),
        ],
    )

    assert rollup.state is ProjectRailState.GREEN
    assert rollup.badge == "🟢"
    assert rollup.actionable_key == "project:demo:issues"


def test_rollup_working_when_automated_work_can_advance() -> None:
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "in_progress"),
            _task(2, "review", node="autoreview"),
        ],
    )

    assert rollup.state is ProjectRailState.WORKING
    assert rollup.badge == "⚙️"
    assert rollup.actionable_key is None


def test_rollup_unbadged_when_all_tasks_are_terminal() -> None:
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "done"),
            _task(2, "accepted"),
            _task(3, "cancelled"),
        ],
    )

    assert rollup.state is ProjectRailState.NONE
    assert rollup.badge is None
    assert rollup.sort_rank == 4


def test_plan_blocked_project_is_red_when_no_task_can_advance() -> None:
    rollup = rollup_project_state(
        "demo",
        [_task(1, "queued")],
        plan_blocked=True,
    )

    assert rollup.state is ProjectRailState.RED
    assert rollup.reason == "plan-blocked"


def test_plan_blocked_project_allows_planner_task_to_advance() -> None:
    rollup = rollup_project_state(
        "demo",
        [_task(1, "queued", flow="plan_project")],
        plan_blocked=True,
    )

    assert rollup.state is ProjectRailState.WORKING


def test_actionable_alert_prefixes_contribute_to_waiting_rollup() -> None:
    alerts = [
        SimpleNamespace(alert_type="stuck_on_task:demo/1"),
        SimpleNamespace(alert_type="no_session_for_assignment:other/2"),
    ]
    rollup = rollup_project_state(
        "demo",
        [_task(1, "in_progress"), _task(2, "in_progress")],
        actionable_task_alert_ids=actionable_alert_task_ids(alerts, project_key="demo"),
    )

    assert rollup.state is ProjectRailState.YELLOW
    assert rollup.badge == "🟡"


def test_surfaceable_operational_alert_taxonomy_keeps_user_action_signals_visible() -> None:
    assert is_surfaceable_operational_alert("stuck_on_task:demo/1")
    assert is_surfaceable_operational_alert("no_session_for_assignment:demo/2")
    assert not is_surfaceable_operational_alert("pane:auth_expired")
