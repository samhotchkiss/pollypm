"""Focused regressions for hold/resume across review nodes."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from pollypm.work.cli import task_app
from pollypm.work.models import ExecutionStatus, WorkStatus
from pollypm.work.service_support import InvalidTransitionError
from pollypm.work.sqlite_service import SQLiteWorkService


runner = CliRunner()


def _review_ready_service(tmp_path):
    db_path = tmp_path / "work.db"
    svc = SQLiteWorkService(db_path=db_path)
    task = svc.create(
        title="Needs review",
        description="Exercise hold/resume over review",
        type="task",
        project="proj",
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "agent-1")
    svc.node_done(
        task.task_id,
        "agent-1",
        {
            "type": "code_change",
            "summary": "Implemented the feature",
            "artifacts": [
                {
                    "kind": "commit",
                    "description": "feat: implementation",
                    "ref": "abc123",
                }
            ],
        },
    )
    return svc, task.task_id, str(db_path)


def _plan_review_service(tmp_path):
    db_path = tmp_path / "plan.db"
    svc = SQLiteWorkService(db_path=db_path)
    task = svc.create(
        title="Human review task",
        description="Exercise human approval review hold guard",
        type="task",
        project="proj",
        flow_template="user-review",
        roles={"worker": "agent-1", "user": "sam"},
        priority="high",
        created_by="tester",
    )
    svc.queue(task.task_id, "pm")
    svc.claim(task.task_id, "agent-1")
    svc.node_done(
        task.task_id,
        "agent-1",
        {
            "type": "code_change",
            "summary": "ready for human review",
            "artifacts": [
                {
                    "kind": "note",
                    "description": "review artifact",
                    "ref": "stage-1",
                }
            ],
        },
    )
    return svc, task.task_id, str(db_path)


def test_hold_and_resume_round_trip_review_tasks(tmp_path):
    svc, task_id, _db_path = _review_ready_service(tmp_path)

    review_before = svc.get_execution(task_id, node_id="code_review")
    assert len(review_before) == 1
    assert review_before[0].status == ExecutionStatus.ACTIVE

    held = svc.hold(task_id, "pm", "waiting for reviewer bandwidth")
    assert held.work_status == WorkStatus.ON_HOLD
    assert held.current_node_id == "code_review"
    assert held.transitions[-1].from_state == "review"
    assert held.transitions[-1].to_state == "on_hold"
    assert held.transitions[-1].reason == "waiting for reviewer bandwidth"

    resumed = svc.resume(task_id, "pm")
    assert resumed.work_status == WorkStatus.REVIEW
    assert resumed.current_node_id == "code_review"
    assert resumed.transitions[-1].from_state == "on_hold"
    assert resumed.transitions[-1].to_state == "review"

    review_after = svc.get_execution(task_id, node_id="code_review")
    assert len(review_after) == 1
    assert review_after[0].status == ExecutionStatus.ACTIVE


def test_cli_hold_reason_and_resume_restore_review(tmp_path):
    svc, task_id, db_path = _review_ready_service(tmp_path)

    hold = runner.invoke(
        task_app,
        [
            "hold",
            task_id,
            "--actor",
            "pm",
            "--reason",
            "reviewer is out today",
            "--db",
            db_path,
        ],
    )
    assert hold.exit_code == 0, hold.output
    assert f"On hold: {task_id}" in hold.output

    held = svc.get(task_id)
    assert held.work_status == WorkStatus.ON_HOLD
    assert held.transitions[-1].reason == "reviewer is out today"

    resume = runner.invoke(
        task_app,
        [
            "resume",
            task_id,
            "--actor",
            "pm",
            "--db",
            db_path,
        ],
    )
    assert resume.exit_code == 0, resume.output
    assert f"Resumed {task_id}" in resume.output

    resumed = svc.get(task_id)
    assert resumed.work_status == WorkStatus.REVIEW
    assert resumed.current_node_id == "code_review"


def test_cli_repair_review_hold_restores_review_with_audit_context(tmp_path):
    svc, task_id, db_path = _review_ready_service(tmp_path)
    svc.hold(task_id, "pm", "reviewer slot race")

    repair = runner.invoke(
        task_app,
        [
            "repair",
            task_id,
            "--case",
            "review-hold",
            "--reason",
            "review notification should remain actionable",
            "--actor",
            "pm",
            "--db",
            db_path,
        ],
    )

    assert repair.exit_code == 0, repair.output
    assert f"Repaired {task_id}: on_hold -> review at code_review" in repair.output
    repaired = svc.get(task_id)
    assert repaired.work_status == WorkStatus.REVIEW
    assert repaired.current_node_id == "code_review"
    context = svc.get_context(task_id, limit=5)
    assert context[-1].actor == "pm"
    assert "repair review-hold" in context[-1].text
    assert "review notification should remain actionable" in context[-1].text


def test_cli_repair_review_hold_rejects_non_review_hold(tmp_path):
    _svc, task_id, db_path = _review_ready_service(tmp_path)

    repair = runner.invoke(
        task_app,
        [
            "repair",
            task_id,
            "--case",
            "review-hold",
            "--reason",
            "not actually held",
            "--db",
            db_path,
        ],
    )

    assert repair.exit_code == 1
    assert "not 'on_hold'" in repair.output


def test_auto_hold_rejects_review_ready_notify_subjects(tmp_path):
    svc, task_id, _db_path = _review_ready_service(tmp_path)

    with pytest.raises(
        InvalidTransitionError,
        match="review-ready task from a non-blocking",
    ):
        svc.hold(
            task_id,
            "pm",
            "Waiting on operator: [Action] Done: Phase 2 scraper runtime handed to review",
        )

    task = svc.get(task_id)
    assert task.work_status == WorkStatus.REVIEW
    assert task.current_node_id == "code_review"


def test_auto_hold_rejects_human_user_approval_nodes(tmp_path):
    svc, task_id, _db_path = _plan_review_service(tmp_path)

    with pytest.raises(
        InvalidTransitionError,
        match="human review/approval node",
    ):
        svc.hold(
            task_id,
            "pm",
            "Waiting on operator: [Action] BookTalk plan at user_approval — need your call on the '5 agents' issue",
        )

    task = svc.get(task_id)
    assert task.work_status == WorkStatus.REVIEW
    assert task.current_node_id == "human_review"
