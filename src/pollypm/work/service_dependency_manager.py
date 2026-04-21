"""Dependency manager for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` and task identifiers for dependency
  relationships and blocker resolution.
- Outputs: dependency-mutating side effects and dependent-task reads.
- Side effects: groups the dependency boundary behind one service-owned
  facade so callers do not need to know the helper layout.
- Invariants: behavior stays delegated to the existing dependency
  helpers; the manager only centralizes the service-facing orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pollypm.work.models import Task
from pollypm.work.service_dependencies import (
    check_auto_unblock,
    dependent_tasks,
    has_unresolved_blockers,
    link_tasks,
    maybe_block,
    maybe_unblock,
    on_cancelled,
    unlink_tasks,
    would_create_cycle,
)

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


@dataclass(slots=True)
class WorkDependencyManager:
    """Facade for the dependency/blocking service boundary."""

    service: "SQLiteWorkService"

    def link(self, from_id: str, to_id: str, kind: str) -> None:
        link_tasks(self.service, from_id, to_id, kind)

    def unlink(self, from_id: str, to_id: str, kind: str) -> None:
        unlink_tasks(self.service, from_id, to_id, kind)

    def dependents(self, task_id: str) -> list[Task]:
        return dependent_tasks(self.service, task_id)

    def would_create_cycle(
        self,
        from_project: str,
        from_number: int,
        to_project: str,
        to_number: int,
    ) -> bool:
        return would_create_cycle(
            self.service,
            from_project,
            from_number,
            to_project,
            to_number,
        )

    def has_unresolved_blockers(self, task_id: str) -> bool:
        return has_unresolved_blockers(self.service, task_id)

    def maybe_block(self, task_id: str) -> None:
        maybe_block(self.service, task_id)

    def maybe_unblock(self, task_id: str) -> None:
        maybe_unblock(self.service, task_id)

    def check_auto_unblock(self, task_id: str) -> None:
        check_auto_unblock(self.service, task_id)

    def on_cancelled(self, task_id: str) -> None:
        on_cancelled(self.service, task_id)
