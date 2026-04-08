from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    title: str
    state: str
    path: Path


class TaskBackend(Protocol):
    def issues_root(self) -> Path: ...

    def exists(self) -> bool: ...

    def ensure_tracker(self) -> Path: ...

    def list_tasks(self, *, states: list[str] | None = None) -> list[TaskRecord]: ...

    def create_task(self, *, title: str, body: str = "", state: str = "01-ready") -> TaskRecord: ...

    def move_task(self, task_id: str, to_state: str) -> TaskRecord: ...

    def append_note(self, name: str, text: str) -> Path: ...

    def latest_issue_number(self) -> int: ...

    def state_counts(self) -> dict[str, int]: ...
