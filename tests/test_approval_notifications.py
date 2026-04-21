from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pollypm.approval_notifications import (
    format_task_approval_message,
    notify_task_approved,
)
from pollypm.work.models import Priority, Task, TaskType


def _task(*, created_at: datetime | None) -> Task:
    return Task(
        project="notesy",
        task_number=1,
        title="Ship task review polish",
        type=TaskType.TASK,
        priority=Priority.HIGH,
        created_at=created_at,
    )


def test_format_task_approval_message_includes_compact_ship_time() -> None:
    approved_at = datetime(2026, 4, 21, 18, 45, tzinfo=UTC)
    task = _task(created_at=approved_at - timedelta(minutes=45))

    assert (
        format_task_approval_message(task, approved_at=approved_at)
        == "\u2713 notesy/1 approved - shipped in 45m"
    )


def test_notify_task_approved_emits_toast_and_macos_banner() -> None:
    approved_at = datetime(2026, 4, 21, 18, 45, tzinfo=UTC)
    task = _task(created_at=approved_at - timedelta(hours=2, minutes=5))
    toast_calls: list[tuple[str, str, float]] = []
    adapter_calls: list[tuple[str, str, str, str]] = []

    class FakeAdapter:
        def is_available(self) -> bool:
            return True

        def notify(
            self,
            *,
            title: str,
            body: str,
            task_id: str,
            project: str,
        ) -> None:
            adapter_calls.append((title, body, task_id, project))

    message = notify_task_approved(
        task,
        notify=lambda msg, *, severity, timeout: toast_calls.append(
            (msg, severity, timeout)
        ),
        approved_at=approved_at,
        os_adapter=FakeAdapter(),
    )

    assert message == "\u2713 notesy/1 approved - shipped in 2h 5m"
    assert toast_calls == [
        ("\u2713 notesy/1 approved - shipped in 2h 5m", "information", 5.0)
    ]
    assert adapter_calls == [
        (
            "PollyPM: Task approved",
            "\u2713 notesy/1 approved - shipped in 2h 5m",
            "notesy/1",
            "notesy",
        )
    ]
