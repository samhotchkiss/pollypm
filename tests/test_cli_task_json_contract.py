from __future__ import annotations

from datetime import UTC, datetime

from pollypm.work.cli import _task_to_dict
from pollypm.work.models import (
    Priority,
    Task,
    TaskType,
    Transition,
    WorkStatus,
)


def test_task_to_dict_includes_rest_shaped_transitions_oldest_first() -> None:
    late = datetime(2026, 5, 25, 12, 5, tzinfo=UTC)
    early = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    task = Task(
        project="demo",
        task_number=1,
        title="Serialize me",
        type=TaskType.TASK,
        work_status=WorkStatus.IN_PROGRESS,
        priority=Priority.HIGH,
        transitions=[
            Transition(
                from_state="queued",
                to_state="in_progress",
                actor="worker",
                timestamp=late,
                reason="claim",
            ),
            Transition(
                from_state="draft",
                to_state="queued",
                actor="pm",
                timestamp=early,
                reason=None,
            ),
        ],
    )

    payload = _task_to_dict(task)

    assert payload["transitions"] == [
        {
            "from_state": "draft",
            "to_state": "queued",
            "actor": "pm",
            "timestamp": "2026-05-25T12:00:00+00:00",
            "reason": None,
        },
        {
            "from_state": "queued",
            "to_state": "in_progress",
            "actor": "worker",
            "timestamp": "2026-05-25T12:05:00+00:00",
            "reason": "claim",
        },
    ]
