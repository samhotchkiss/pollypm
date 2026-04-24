from __future__ import annotations

from typer.testing import CliRunner

from pollypm.work.cli import task_app
from pollypm.work.inbox_view import inbox_tasks
from pollypm.work.models import WorkStatus
from pollypm.work.service_support import InvalidTransitionError
from pollypm.work.sqlite_service import SQLiteWorkService


runner = CliRunner()


def _service(tmp_path):
    return SQLiteWorkService(db_path=tmp_path / "state.db", project_path=tmp_path)


def test_requires_human_review_materializes_user_inbox_task(tmp_path):
    svc = _service(tmp_path)
    try:
        task = svc.create(
            title="Dangerous deploy",
            description="Deploy production changes.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            requires_human_review=True,
        )

        try:
            svc.queue(task.task_id, "polly")
        except InvalidTransitionError as exc:
            assert "Created user inbox task: demo/2" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("queue should require human review")

        approval = svc.get("demo/2")
        assert approval.work_status == WorkStatus.DRAFT
        assert "human_review_request" in approval.labels
        assert f"target_task:{task.task_id}" in approval.labels
        assert approval.roles["requester"] == "user"
        assert [item.task_id for item in inbox_tasks(svc, project="demo")] == ["demo/2"]
    finally:
        svc.close()


def test_human_review_approval_unlocks_queue_and_closes_request(tmp_path):
    svc = _service(tmp_path)
    try:
        task = svc.create(
            title="Dangerous deploy",
            description="Deploy production changes.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            requires_human_review=True,
        )
        try:
            svc.queue(task.task_id, "polly")
        except InvalidTransitionError:
            pass

        svc.approve_human_review(task.task_id, "user", "looks safe")
        queued = svc.queue(task.task_id, "polly")

        assert queued.work_status == WorkStatus.QUEUED
        assert svc.get("demo/2").work_status == WorkStatus.DONE
        context = svc.get_context(task.task_id, entry_type="human_review_approved")
        assert context[-1].text == "looks safe"
    finally:
        svc.close()


def test_operator_fast_track_requires_explicit_authorization(tmp_path):
    svc = _service(tmp_path)
    try:
        task = svc.create(
            title="Fast track deploy",
            description="Deploy production changes.",
            type="task",
            project="demo",
            flow_template="standard",
            roles={"worker": "worker", "reviewer": "reviewer"},
            requires_human_review=True,
        )

        try:
            svc.approve_human_review(task.task_id, "polly", "authorized")
        except InvalidTransitionError as exc:
            assert "--fast-track-authorized" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("operator approval should require explicit fast-track")

        svc.approve_human_review(
            task.task_id,
            "polly",
            "standing project authorization",
            fast_track_authorized=True,
        )
        queued = svc.queue(task.task_id, "polly")
        assert queued.work_status == WorkStatus.QUEUED
    finally:
        svc.close()


def test_cli_queue_readiness_warning_is_coaching_not_gate(tmp_path):
    db_path = str(tmp_path / "state.db")
    create = runner.invoke(
        task_app,
        [
            "create",
            "Loose task",
            "--project",
            "demo",
            "--description",
            "Figure out the implementation.",
            "--role",
            "worker=worker",
            "--role",
            "reviewer=reviewer",
            "--db",
            db_path,
        ],
    )
    assert create.exit_code == 0, create.output
    assert "Readiness: Task is queueable but underspecified" in create.output

    queue = runner.invoke(task_app, ["queue", "demo/1", "--db", db_path])
    assert queue.exit_code == 0, queue.output
    assert "Queued demo/1" in queue.output
    assert "missing acceptance criteria" in queue.output
