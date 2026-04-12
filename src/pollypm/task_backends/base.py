from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


TRACKER_STATES = [
    "00-not-ready",
    "01-ready",
    "02-in-progress",
    "03-needs-review",
    "04-in-review",
    "05-completed",
]


class InvalidTaskTransition(ValueError):
    """Raised when a task moves outside the allowed six-state pipeline."""


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    title: str
    state: str
    path: Path


def validate_task_transition(current_state: str, to_state: str) -> None:
    if current_state == to_state:
        return
    if current_state not in TRACKER_STATES:
        raise InvalidTaskTransition(f"Unknown current state: {current_state}")
    if to_state not in TRACKER_STATES:
        raise InvalidTaskTransition(f"Unknown destination state: {to_state}")

    if to_state == "05-completed" and current_state != "04-in-review":
        raise InvalidTaskTransition(
            f"Invalid transition {current_state} -> {to_state}; issues must pass through 04-in-review before completion"
        )

    from_idx = TRACKER_STATES.index(current_state)
    to_idx = TRACKER_STATES.index(to_state)
    if to_idx == from_idx + 1:
        return

    if to_state == "02-in-progress" and current_state in {"03-needs-review", "04-in-review", "05-completed"}:
        return

    if to_idx > from_idx + 1:
        skipped = TRACKER_STATES[from_idx + 1 : to_idx]
        raise InvalidTaskTransition(
            f"Invalid transition {current_state} -> {to_state}; skipped states: {', '.join(skipped)}"
        )

    raise InvalidTaskTransition(
        f"Invalid transition {current_state} -> {to_state}; only request-changes or reopen may move backward to 02-in-progress"
    )


class TaskBackend(Protocol):
    def issues_root(self) -> Path: ...

    def exists(self) -> bool: ...

    def ensure_tracker(self) -> Path: ...

    def list_tasks(self, *, states: list[str] | None = None) -> list[TaskRecord]: ...

    def get_task(self, task_id: str) -> TaskRecord: ...

    def next_available(self) -> TaskRecord | None: ...

    def task_history(self, task_id: str) -> list[str]: ...

    def create_task(self, *, title: str, body: str = "", state: str = "01-ready") -> TaskRecord: ...

    def move_task(self, task_id: str, to_state: str) -> TaskRecord: ...

    def read_task(self, task: TaskRecord) -> str: ...

    def append_note(self, name: str, text: str) -> Path: ...

    def latest_issue_number(self) -> int: ...

    def state_counts(self) -> dict[str, int]: ...
