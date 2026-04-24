"""Regression tests for plan_project critic_panel child enforcement (#778)."""

from __future__ import annotations

import pytest

from pollypm.work.models import WorkStatus
from pollypm.work.sqlite_service import SQLiteWorkService, ValidationError


EXPECTED_CRITICS = (
    "critic_simplicity",
    "critic_maintainability",
    "critic_user",
    "critic_operational",
    "critic_security",
)


@pytest.fixture
def svc(tmp_path):
    return SQLiteWorkService(db_path=tmp_path / "work.db", project_path=tmp_path)


def _stage_output(stage: str) -> dict:
    return {
        "type": "document",
        "summary": f"{stage} complete",
        "artifacts": [
            {
                "kind": "file_change",
                "description": f"{stage} artifact",
                "path": f"docs/plan/{stage}.md",
            }
        ],
    }


def _critic_output(critic: str) -> dict:
    return {
        "type": "document",
        "summary": f"{critic} structured critique",
        "artifacts": [
            {
                "kind": "note",
                "description": f"{critic} scored all candidate plans",
            }
        ],
    }


def _create_plan_at_critic_panel(svc: SQLiteWorkService) -> str:
    task = svc.create(
        title="Plan project",
        description="Run the architect planning flow.",
        type="task",
        project="demo",
        flow_template="plan_project",
        roles={"architect": "architect"},
        priority="high",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "architect")
    for stage in ("research", "discover", "decompose", "test_strategy", "magic"):
        svc.node_done(task.task_id, "architect", _stage_output(stage))
    assert svc.get(task.task_id).current_node_id == "critic_panel"
    return task.task_id


def _create_critic_child(
    svc: SQLiteWorkService,
    parent_id: str,
    critic: str,
    *,
    finish: bool = True,
    with_output: bool = True,
) -> str:
    child = svc.create(
        title=f"{critic} critique",
        description="Review all candidate plans.",
        type="task",
        project="demo",
        flow_template="critique_flow",
        roles={"critic": critic, "requester": "architect"},
        priority="high",
        created_by="architect",
    )
    svc.link(parent_id, child.task_id, "parent")
    svc.queue(child.task_id, "architect")
    svc.claim(child.task_id, "architect")
    if finish and with_output:
        svc.node_done(
            child.task_id,
            critic,
            _critic_output(critic),
            skip_gates=True,
        )
    elif finish:
        svc.mark_done(child.task_id, "test")
    return child.task_id


def test_zero_child_critic_panel_cannot_complete_even_with_skip_gates(
    svc: SQLiteWorkService,
) -> None:
    plan_id = _create_plan_at_critic_panel(svc)

    with pytest.raises(ValidationError, match="expected exactly five"):
        svc.node_done(
            plan_id,
            "architect",
            _stage_output("critic_panel"),
            skip_gates=True,
        )

    plan = svc.get(plan_id)
    assert plan.current_node_id == "critic_panel"
    assert plan.work_status == WorkStatus.IN_PROGRESS


def test_critic_panel_requires_done_children_with_structured_outputs(
    svc: SQLiteWorkService,
) -> None:
    plan_id = _create_plan_at_critic_panel(svc)
    for critic in EXPECTED_CRITICS:
        _create_critic_child(svc, plan_id, critic)

    result = svc.node_done(plan_id, "architect", _stage_output("critic_panel"))

    assert result.current_node_id == "synthesize"
    context = [
        entry
        for entry in result.context
        if entry.entry_type == "critic_panel_children"
    ]
    assert len(context) == 1
    for critic in EXPECTED_CRITICS:
        assert f"{critic}=demo/" in context[0].text


def test_critic_panel_rejects_cancelled_child(
    svc: SQLiteWorkService,
) -> None:
    plan_id = _create_plan_at_critic_panel(svc)
    cancelled_child = _create_critic_child(svc, plan_id, EXPECTED_CRITICS[0])
    child = svc.get(cancelled_child)
    svc._conn.execute(
        "UPDATE work_tasks SET work_status = ? "
        "WHERE project = ? AND task_number = ?",
        (WorkStatus.CANCELLED.value, child.project, child.task_number),
    )
    svc._conn.commit()
    for critic in EXPECTED_CRITICS[1:]:
        _create_critic_child(svc, plan_id, critic)

    with pytest.raises(ValidationError, match="expected 'done'"):
        svc.node_done(plan_id, "architect", _stage_output("critic_panel"))


def test_critic_panel_rejects_done_child_without_structured_output(
    svc: SQLiteWorkService,
) -> None:
    plan_id = _create_plan_at_critic_panel(svc)
    _create_critic_child(
        svc,
        plan_id,
        EXPECTED_CRITICS[0],
        with_output=False,
    )
    for critic in EXPECTED_CRITICS[1:]:
        _create_critic_child(svc, plan_id, critic)

    with pytest.raises(ValidationError, match="structured critique output"):
        svc.node_done(plan_id, "architect", _stage_output("critic_panel"))
