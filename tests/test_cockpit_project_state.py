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


def test_rollup_yellow_when_all_nonterminal_tasks_wait_on_user() -> None:
    """Per the v1 dashboard contract, red is reserved for operational
    fault states. A project where every active task is waiting on the
    user has user-attention work to do, but nothing is *broken* — that
    is yellow, not a false red alarm."""
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "blocked"),
            _task(2, "waiting_on_user"),
            _task(3, "done"),
        ],
    )

    assert rollup.state is ProjectRailState.YELLOW
    assert rollup.badge == "🟡"
    assert rollup.actionable_key == "project:demo:issues"


def test_rollup_red_only_when_operational_alert_present() -> None:
    """A stuck-on-task or missing-session alert is a true fault state —
    the system itself can't recover without user intervention. Those
    must surface as red even when the rest of the project is moving."""
    alerts = [SimpleNamespace(alert_type="stuck_on_task:demo/1")]
    rollup = rollup_project_state(
        "demo",
        [_task(1, "in_progress"), _task(2, "in_progress")],
        actionable_task_alert_ids=actionable_alert_task_ids(
            alerts, project_key="demo",
        ),
    )

    assert rollup.state is ProjectRailState.RED
    assert rollup.badge == "🔴"
    assert rollup.reason == "operational alert needs review"


def test_rollup_yellow_when_stuck_alert_is_on_a_user_waiting_task() -> None:
    """A ``stuck_on_task:<id>`` alert fires when a session sits idle
    waiting for the user. When the underlying task is *already* in a
    user-waiting state (blocked / on_hold / waiting_on_user), the
    alert is just the same fact in different words — flagging the
    rail RED then tells the user "something is broken" when the
    system is doing exactly what it should: waiting on them.

    Drop the stuck alert from the RED trigger when the task is
    already user-waiting; let the rollup fall through to YELLOW.
    """
    alerts = [SimpleNamespace(alert_type="stuck_on_task:demo/1")]
    rollup = rollup_project_state(
        "demo",
        # Task #1 is already blocked (user-waiting); the stuck alert
        # on it must not push the rail to RED.
        [_task(1, "blocked"), _task(2, "in_progress")],
        actionable_task_alert_ids=actionable_alert_task_ids(
            alerts, project_key="demo",
        ),
    )

    assert rollup.state is ProjectRailState.YELLOW
    assert rollup.badge == "🟡"


def test_rollup_red_when_stuck_alert_is_on_advancing_task() -> None:
    """When the alerted task is *not* user-waiting (it's mid-flight
    in_progress, queued, or in autoreview), the stuck signal is a
    real fault — keep RED."""
    alerts = [SimpleNamespace(alert_type="stuck_on_task:demo/1")]
    rollup = rollup_project_state(
        "demo",
        # Task #1 is in progress, alerted as stuck → genuinely
        # broken; rail stays RED.
        [_task(1, "in_progress"), _task(2, "in_progress")],
        actionable_task_alert_ids=actionable_alert_task_ids(
            alerts, project_key="demo",
        ),
    )

    assert rollup.state is ProjectRailState.RED


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


def test_rollup_unbadged_when_all_tasks_are_draft() -> None:
    rollup = rollup_project_state(
        "demo",
        [
            _task(1, "draft", flow="plan_project"),
            _task(2, "draft", flow="chat"),
        ],
        plan_blocked=True,
    )

    assert rollup.state is ProjectRailState.NONE
    assert rollup.badge is None


def test_plan_blocked_project_is_not_red_without_user_action() -> None:
    rollup = rollup_project_state(
        "demo",
        [_task(1, "queued")],
        plan_blocked=True,
    )

    assert rollup.state is ProjectRailState.WORKING
    assert rollup.badge == "⚙️"
    assert rollup.reason == "plan needed before automated work"


def test_plan_blocked_project_allows_planner_task_to_advance() -> None:
    rollup = rollup_project_state(
        "demo",
        [_task(1, "queued", flow="plan_project")],
        plan_blocked=True,
    )

    assert rollup.state is ProjectRailState.WORKING


def test_actionable_alert_prefixes_drive_red_state() -> None:
    """Operational alert prefixes (``stuck_on_task:``, etc.) flag the
    rail red — the system has detected a fault that needs the user."""
    alerts = [
        SimpleNamespace(alert_type="stuck_on_task:demo/1"),
        SimpleNamespace(alert_type="no_session_for_assignment:other/2"),
    ]
    rollup = rollup_project_state(
        "demo",
        [_task(1, "in_progress"), _task(2, "in_progress")],
        actionable_task_alert_ids=actionable_alert_task_ids(alerts, project_key="demo"),
    )

    assert rollup.state is ProjectRailState.RED
    assert rollup.badge == "🔴"


def test_surfaceable_operational_alert_taxonomy_keeps_user_action_signals_visible() -> None:
    assert is_surfaceable_operational_alert("stuck_on_task:demo/1")
    assert is_surfaceable_operational_alert("no_session_for_assignment:demo/2")
    assert not is_surfaceable_operational_alert("pane:auth_expired")
