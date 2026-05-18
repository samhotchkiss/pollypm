"""Flow transition helpers for the SQLite work service.

Contract:
- Inputs: a work-service collaborator (see :class:`_WorkService` below)
  plus task ids, flow nodes, and actor metadata.
- Outputs: visit counters and post-transition side effects.
- Side effects: mutates task/execution state and triggers notification
  hooks after commits.
- Invariants: transition side effects stay owned by the work service.

The helpers in this module are parameterised over a structural
:class:`typing.Protocol` rather than the concrete
``pollypm.work.sqlite_service.SQLiteWorkService`` class. This breaks
the import cycle flagged in #1367 (the service top-imports this module,
so a reverse type-annotation import — even guarded with
``TYPE_CHECKING`` — registers as a cycle in the AST boundary scan).
``SQLiteWorkService`` satisfies :class:`_WorkService` structurally; no
runtime registration or subclass change is required.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Protocol

from pollypm.work.models import (
    ExecutionStatus,
    FlowNode,
    FlowTemplate,
    NodeType,
    Task,
    WorkStatus,
    TERMINAL_STATUSES,
)
from pollypm.work.service_support import InvalidTransitionError, _now


class _WorkService(Protocol):
    """Structural view of the SQLite work service used by transition helpers.

    Captures the exact slice these helpers reach into so the
    ``service_transitions <-> sqlite_service`` cycle can be broken without
    pulling the full concrete class into this module's type graph
    (#1367 wedge).
    """

    _conn: sqlite3.Connection
    _project_path: Path | None

    def _record_transition(
        self,
        project: str,
        task_number: int,
        from_state: str,
        to_state: str,
        actor: str,
    ) -> None: ...

    def _resolve_node_assignee(self, task: Task, node: FlowNode) -> str | None: ...

    def _check_auto_unblock(self, task_id: str) -> None: ...

    def _on_task_done(self, task_id: str, actor: str) -> None: ...

    def get(self, task_id: str) -> Task: ...


logger = logging.getLogger(__name__)


def next_visit(service: _WorkService, project: str, task_number: int, node_id: str) -> int:
    row = service._conn.execute(
        "SELECT COALESCE(MAX(visit), 0) AS max_v "
        "FROM work_node_executions "
        "WHERE task_project = ? AND task_number = ? AND node_id = ?",
        (project, task_number, node_id),
    ).fetchone()
    return row["max_v"] + 1


def current_node_visit(service: _WorkService, project: str, task_number: int, node_id: str) -> int:
    row = service._conn.execute(
        "SELECT COALESCE(MAX(visit), 0) AS max_v "
        "FROM work_node_executions "
        "WHERE task_project = ? AND task_number = ? AND node_id = ?",
        (project, task_number, node_id),
    ).fetchone()
    return int(row["max_v"] or 0)


def advance_to_node(
    service: _WorkService,
    task: Task,
    flow: FlowTemplate,
    next_node_id: str | None,
    actor: str,
    from_status: WorkStatus,
) -> None:
    now = _now()
    if next_node_id is None:
        raise InvalidTransitionError("No next node defined.")

    next_node = flow.nodes.get(next_node_id)
    if next_node is None:
        raise InvalidTransitionError(f"Next node '{next_node_id}' not found in flow.")

    if next_node.type == NodeType.TERMINAL:
        service._conn.execute(
            "UPDATE work_tasks SET work_status = ?, current_node_id = NULL, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (WorkStatus.DONE.value, now, task.project, task.task_number),
        )
        service._record_transition(
            task.project,
            task.task_number,
            from_status.value,
            WorkStatus.DONE.value,
            actor,
        )
        return

    new_status = (
        WorkStatus.REVIEW if next_node.type == NodeType.REVIEW else WorkStatus.IN_PROGRESS
    )
    next_assignee = service._resolve_node_assignee(task, next_node)
    visit = next_visit(service, task.project, task.task_number, next_node_id)
    service._conn.execute(
        "UPDATE work_tasks SET work_status = ?, assignee = ?, current_node_id = ?, updated_at = ? "
        "WHERE project = ? AND task_number = ?",
        (
            new_status.value,
            next_assignee,
            next_node_id,
            now,
            task.project,
            task.task_number,
        ),
    )
    service._conn.execute(
        "INSERT INTO work_node_executions "
        "(task_project, task_number, node_id, visit, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            task.project,
            task.task_number,
            next_node_id,
            visit,
            ExecutionStatus.ACTIVE.value,
            now,
        ),
    )
    service._record_transition(
        task.project,
        task.task_number,
        from_status.value,
        new_status.value,
        actor,
    )


def on_task_done(service: _WorkService, task_id: str, actor: str) -> None:
    try:
        from pollypm.notification_staging import check_and_flush_on_done

        task = service.get(task_id)
        check_and_flush_on_done(
            service,
            project=task.project,
            completed_task=task,
            actor=actor or "polly",
            project_path=service._project_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "notification-digest flush skipped for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )

    try:
        from pollypm.task_shipped import emit_task_shipped_card

        emit_task_shipped_card(service, task_id, actor or "polly")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "task_shipped card emit skipped for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )

    # #1511 — backstop emit for plan-shaped tasks that finished outside
    # the ``plan_project`` flow. Plan-review approval cards used to depend
    # on the architect's reflection node (#1399) firing inside that flow;
    # tasks completed on ``standard`` / other flows reached done with no
    # ``plan_review`` row in the messages table, so the cockpit's 2-line
    # approval card never rendered. The hook is no-op for plan_project
    # tasks (the reflection node owns the canonical emit) and for tasks
    # that already have an open plan_review row.
    try:
        from pollypm.work.plan_review_emit import (
            maybe_emit_plan_review_on_task_done,
        )

        maybe_emit_plan_review_on_task_done(service, task_id, actor or "polly")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "plan_review backstop emit skipped for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )


def on_task_transition(
    service: _WorkService,
    task_id: str,
    from_state: str,
    to_state: str,
    actor: str,
) -> None:
    if from_state == "done" and to_state != "done":
        try:
            from pollypm.notification_staging import check_regression_on_reopen

            project = task_id.rsplit("/", 1)[0] if "/" in task_id else ""
            if project:
                check_regression_on_reopen(
                    service,
                    project=project,
                    task_id=task_id,
                    from_state=from_state,
                    to_state=to_state,
                    actor=actor or "system",
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "regression notify skipped for %s (%s→%s): %s",
                task_id,
                from_state,
                to_state,
                exc,
                exc_info=True,
            )


def mark_done(service: _WorkService, task_id: str, actor: str) -> Task:
    task = service.get(task_id)
    if task.work_status in TERMINAL_STATUSES:
        raise InvalidTransitionError(
            f"Cannot mark done task in terminal state '{task.work_status.value}'."
        )

    now = _now()
    from_state = task.work_status.value
    service._record_transition(
        task.project,
        task.task_number,
        from_state,
        WorkStatus.DONE.value,
        actor,
    )
    service._conn.execute(
        "UPDATE work_tasks SET work_status = ?, updated_at = ? "
        "WHERE project = ? AND task_number = ?",
        (WorkStatus.DONE.value, now, task.project, task.task_number),
    )
    service._conn.commit()
    service._check_auto_unblock(task_id)
    service._on_task_done(task_id, actor)
    return service.get(task_id)
