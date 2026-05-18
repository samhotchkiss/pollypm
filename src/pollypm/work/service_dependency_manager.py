"""Dependency manager for the SQLite work service.

Contract:
- Inputs: a work-service collaborator (see :class:`_WorkService` below)
  and task identifiers for dependency relationships and blocker
  resolution.
- Outputs: dependency-mutating side effects and dependent-task reads.
- Side effects: groups the dependency boundary behind one service-owned
  facade so callers do not need to know the helper layout.
- Invariants: behavior stays delegated to the existing dependency
  helpers; the manager only centralizes the service-facing orchestration.

The facade is parameterised over a structural :class:`typing.Protocol`
rather than the concrete ``pollypm.work.sqlite_service.SQLiteWorkService``
class. This breaks the import cycle flagged in #1367 (the service
top-imports this module, so a reverse type-annotation import — even
guarded with ``TYPE_CHECKING`` — registers as a cycle in the AST
boundary scan). ``SQLiteWorkService`` satisfies :class:`_WorkService`
structurally; no runtime registration or subclass change is required.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

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


class _WorkService(Protocol):
    """Structural view of the SQLite work service used by the dependency facade.

    Captures the exact slice the underlying dependency helpers reach
    into so the ``service_dependency_manager <-> sqlite_service`` cycle
    can be broken without pulling the full concrete class into this
    module's type graph (#1367 wedge).
    """

    _conn: sqlite3.Connection

    def _record_transition(
        self,
        project: str,
        task_number: int,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str,
    ) -> None: ...

    def _load_task_token_sums_bulk(self) -> Any: ...

    def _row_to_task(self, row: Any, *, token_sums: Any) -> Task: ...

    def _sync_transition(self, task: Task, from_state: str, to_state: str) -> None: ...

    def get(self, task_id: str) -> Task: ...

    def add_context(self, task_id: str, actor: str, body: str) -> None: ...


@dataclass(slots=True)
class WorkDependencyManager:
    """Facade for the dependency/blocking service boundary."""

    service: _WorkService

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
