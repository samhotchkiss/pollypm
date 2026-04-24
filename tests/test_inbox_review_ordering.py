"""Focused ordering coverage for review rows in the work-service inbox."""

from __future__ import annotations

from datetime import UTC, datetime

from pollypm.work.inbox_view import inbox_tasks
from pollypm.work.models import (
    ActorType,
    FlowNode,
    FlowTemplate,
    NodeType,
    Priority,
    Task,
    TaskType,
    WorkStatus,
)


def _task(
    number: int,
    *,
    title: str,
    flow_template_id: str,
    current_node_id: str,
    priority: Priority,
    updated_at: datetime,
    roles: dict[str, str] | None = None,
) -> Task:
    return Task(
        project="demo",
        task_number=number,
        title=title,
        type=TaskType.TASK,
        work_status=WorkStatus.REVIEW,
        priority=priority,
        flow_template_id=flow_template_id,
        current_node_id=current_node_id,
        roles=roles or {"requester": "user"},
        updated_at=updated_at,
    )


def _human_review_flow() -> FlowTemplate:
    return FlowTemplate(
        name="human-review",
        description="",
        start_node="human_review",
        nodes={
            "human_review": FlowNode(
                name="human_review",
                type=NodeType.REVIEW,
                actor_type=ActorType.HUMAN,
            ),
        },
    )


def _auto_review_flow() -> FlowTemplate:
    return FlowTemplate(
        name="auto-review",
        description="",
        start_node="critic_panel",
        nodes={
            "critic_panel": FlowNode(
                name="critic_panel",
                type=NodeType.REVIEW,
                actor_type=ActorType.ROLE,
                actor_role="reviewer",
            ),
        },
    )


class _FakeService:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = list(tasks)
        self.flows = {
            "human-review": _human_review_flow(),
            "auto-review": _auto_review_flow(),
        }

    def list_tasks(self, *, project: str | None = None) -> list[Task]:
        if project is None:
            return list(self.tasks)
        return [task for task in self.tasks if task.project == project]

    def get_flow(self, name: str, project: str | None = None) -> FlowTemplate:
        return self.flows[name]


def test_inbox_tasks_sort_user_review_before_autoreview() -> None:
    human_low = _task(
        1,
        title="Human low-priority review",
        flow_template_id="human-review",
        current_node_id="human_review",
        priority=Priority.LOW,
        updated_at=datetime(2026, 4, 20, 16, 0, tzinfo=UTC),
    )
    auto_critical = _task(
        2,
        title="Russell critical autoreview",
        flow_template_id="auto-review",
        current_node_id="critic_panel",
        priority=Priority.CRITICAL,
        updated_at=datetime(2026, 4, 20, 18, 0, tzinfo=UTC),
    )
    human_high = _task(
        3,
        title="Human high-priority review",
        flow_template_id="human-review",
        current_node_id="human_review",
        priority=Priority.HIGH,
        updated_at=datetime(2026, 4, 20, 17, 0, tzinfo=UTC),
    )

    ordered = inbox_tasks(
        _FakeService([auto_critical, human_low, human_high]),
        project="demo",
    )

    assert [task.task_id for task in ordered] == ["demo/3", "demo/1", "demo/2"]
