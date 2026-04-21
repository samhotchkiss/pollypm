from __future__ import annotations

from datetime import UTC, datetime

from pollypm.rejection_feedback import (
    emit_rejection_feedback,
    feedback_target_task_id,
    is_rejection_feedback_task,
    unread_rejection_feedback,
)
from pollypm.work.models import Priority, Task, TaskType, WorkStatus


def _work_task() -> Task:
    return Task(
        project="demo",
        task_number=7,
        title="Ship rejection feedback",
        type=TaskType.TASK,
        work_status=WorkStatus.IN_PROGRESS,
        flow_template_id="standard",
        flow_template_version=1,
        current_node_id="implement",
        assignee="worker_demo",
        priority=Priority.HIGH,
        description="Tighten the reject loop.",
        roles={"worker": "worker_demo", "operator": "polly"},
        created_at=datetime(2026, 4, 20, 17, 0, tzinfo=UTC),
        created_by="polly",
        updated_at=datetime(2026, 4, 20, 17, 10, tzinfo=UTC),
    )


class _CreateSpy:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return kwargs


class _ReadAwareSvc:
    def __init__(self, tasks: list[Task], read_task_ids: set[str] | None = None) -> None:
        self._tasks = tasks
        self._read_task_ids = read_task_ids or set()

    def list_tasks(self, *, project: str | None = None):
        assert project == "demo"
        return list(self._tasks)

    def get_context(self, task_id: str, *, entry_type: str | None = None, limit: int = 1):
        assert limit == 1
        if entry_type == "read" and task_id in self._read_task_ids:
            return [object()]
        return []


def test_emit_rejection_feedback_creates_structured_inbox_item() -> None:
    spy = _CreateSpy()

    emit_rejection_feedback(
        spy,
        task=_work_task(),
        reviewer="polly",
        reason="Need better rollback coverage.\nAnd a guard for stale reads.",
    )

    assert spy.kwargs is not None
    assert spy.kwargs["title"] == "Rejected demo/7 — Ship rejection feedback"
    assert spy.kwargs["flow_template"] == "chat"
    assert spy.kwargs["project"] == "demo"
    assert spy.kwargs["roles"] == {"requester": "polly", "operator": "user"}
    assert "review_feedback" in spy.kwargs["labels"]
    assert "task:demo/7" in spy.kwargs["labels"]
    assert spy.kwargs["description"].splitlines()[0] == "Need better rollback coverage."


def test_unread_rejection_feedback_returns_latest_unread_notice() -> None:
    older = Task(
        project="demo",
        task_number=80,
        title="Rejected demo/7 — older",
        type=TaskType.TASK,
        labels=["review_feedback", "task:demo/7", "project:demo"],
        work_status=WorkStatus.DRAFT,
        flow_template_id="chat",
        flow_template_version=1,
        current_node_id="chat",
        priority=Priority.HIGH,
        description="Older reason",
        roles={"requester": "polly", "operator": "user"},
        created_at=datetime(2026, 4, 20, 17, 10, tzinfo=UTC),
        created_by="polly",
        updated_at=datetime(2026, 4, 20, 17, 10, tzinfo=UTC),
    )
    newer = Task(
        project="demo",
        task_number=81,
        title="Rejected demo/7 — newer",
        type=TaskType.TASK,
        labels=["review_feedback", "task:demo/7", "project:demo"],
        work_status=WorkStatus.DRAFT,
        flow_template_id="chat",
        flow_template_version=1,
        current_node_id="chat",
        priority=Priority.HIGH,
        description="Newest reason",
        roles={"requester": "polly", "operator": "user"},
        created_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
        created_by="polly",
        updated_at=datetime(2026, 4, 20, 17, 20, tzinfo=UTC),
    )
    svc = _ReadAwareSvc([older, newer], read_task_ids={"demo/80"})

    notices = unread_rejection_feedback(svc, project="demo")

    assert list(notices) == ["demo/7"]
    assert notices["demo/7"].inbox_task_id == "demo/81"
    assert notices["demo/7"].preview == "Newest reason"
    assert is_rejection_feedback_task(newer) is True
    assert feedback_target_task_id(newer) == "demo/7"
