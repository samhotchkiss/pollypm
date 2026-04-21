"""Dependency and blocking logic for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` and task identifiers/links.
- Outputs: resolved dependent task lists and blocking-state decisions.
- Side effects: mutates ``work_task_dependencies`` and task status rows.
- Invariants: cycle checks stay centralized; auto-block/unblock remains
  owned by the work service rather than CLI or plugin callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pollypm.work.models import LinkKind, Task, WorkStatus
from pollypm.work.service_support import TaskNotFoundError, ValidationError, _now, _parse_task_id

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


def link_tasks(service: "SQLiteWorkService", from_id: str, to_id: str, kind: str) -> None:
    try:
        link_kind = LinkKind(kind)
    except ValueError as exc:
        raise ValidationError(
            f"Invalid link kind '{kind}'. "
            f"Must be one of: {[item.value for item in LinkKind]}."
        ) from exc

    from_project, from_number = _parse_task_id(from_id)
    to_project, to_number = _parse_task_id(to_id)

    for tid in (from_id, to_id):
        project, number = _parse_task_id(tid)
        row = service._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{tid}' not found.")

    if link_kind is LinkKind.BLOCKS and would_create_cycle(
        service,
        from_project,
        from_number,
        to_project,
        to_number,
    ):
        raise ValidationError("circular dependency detected")

    service._conn.execute(
        "INSERT OR IGNORE INTO work_task_dependencies "
        "(from_project, from_task_number, to_project, to_task_number, kind, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            from_project,
            from_number,
            to_project,
            to_number,
            link_kind.value,
            _now(),
        ),
    )
    service._conn.commit()

    if link_kind is LinkKind.BLOCKS:
        maybe_block(service, to_id)


def unlink_tasks(service: "SQLiteWorkService", from_id: str, to_id: str, kind: str) -> None:
    try:
        link_kind = LinkKind(kind)
    except ValueError as exc:
        raise ValidationError(
            f"Invalid link kind '{kind}'. "
            f"Must be one of: {[item.value for item in LinkKind]}."
        ) from exc

    from_project, from_number = _parse_task_id(from_id)
    to_project, to_number = _parse_task_id(to_id)
    service._conn.execute(
        "DELETE FROM work_task_dependencies "
        "WHERE from_project = ? AND from_task_number = ? "
        "AND to_project = ? AND to_task_number = ? AND kind = ?",
        (from_project, from_number, to_project, to_number, link_kind.value),
    )
    service._conn.commit()

    if link_kind is LinkKind.BLOCKS:
        maybe_unblock(service, to_id)


def dependent_tasks(service: "SQLiteWorkService", task_id: str) -> list[Task]:
    project, number = _parse_task_id(task_id)
    rows = service._conn.execute(
        """
        WITH RECURSIVE deps(project, task_number) AS (
            SELECT to_project, to_task_number
            FROM work_task_dependencies
            WHERE from_project = ? AND from_task_number = ? AND kind = ?
            UNION
            SELECT d.to_project, d.to_task_number
            FROM work_task_dependencies d
            JOIN deps
              ON d.from_project = deps.project
             AND d.from_task_number = deps.task_number
            WHERE d.kind = ?
        )
        SELECT DISTINCT project, task_number
        FROM deps
        ORDER BY project, task_number
        """,
        (project, number, LinkKind.BLOCKS.value, LinkKind.BLOCKS.value),
    ).fetchall()
    task_keys = [(row["project"], row["task_number"]) for row in rows]
    if not task_keys:
        return []

    where = " OR ".join("(project = ? AND task_number = ?)" for _ in task_keys)
    params: list[object] = []
    for task_project, task_number in task_keys:
        params.extend((task_project, task_number))
    task_rows = service._conn.execute(
        f"SELECT * FROM work_tasks WHERE {where} ORDER BY project, task_number",
        params,
    ).fetchall()
    token_sums = service._load_task_token_sums_bulk()
    return [service._row_to_task(row, token_sums=token_sums) for row in task_rows]


def would_create_cycle(
    service: "SQLiteWorkService",
    from_project: str,
    from_number: int,
    to_project: str,
    to_number: int,
) -> bool:
    target = (from_project, from_number)
    visited: set[tuple[str, int]] = set()
    stack: list[tuple[str, int]] = [(to_project, to_number)]

    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        rows = service._conn.execute(
            "SELECT to_project, to_task_number FROM work_task_dependencies "
            "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
            (current[0], current[1], LinkKind.BLOCKS.value),
        ).fetchall()
        for row in rows:
            stack.append((row["to_project"], row["to_task_number"]))

    return False


def has_unresolved_blockers(service: "SQLiteWorkService", task_id: str) -> bool:
    project, number = _parse_task_id(task_id)
    rows = service._conn.execute(
        "SELECT d.from_project, d.from_task_number "
        "FROM work_task_dependencies d "
        "WHERE d.to_project = ? AND d.to_task_number = ? AND d.kind = ?",
        (project, number, LinkKind.BLOCKS.value),
    ).fetchall()
    for row in rows:
        status_row = service._conn.execute(
            "SELECT work_status FROM work_tasks "
            "WHERE project = ? AND task_number = ?",
            (row["from_project"], row["from_task_number"]),
        ).fetchone()
        if status_row and status_row["work_status"] not in (
            WorkStatus.DONE.value,
            WorkStatus.CANCELLED.value,
        ):
            return True
    return False


def maybe_block(service: "SQLiteWorkService", task_id: str) -> None:
    task = service.get(task_id)
    if task.work_status not in (WorkStatus.QUEUED, WorkStatus.IN_PROGRESS):
        return
    if not has_unresolved_blockers(service, task_id):
        return
    now = _now()
    service._record_transition(
        task.project,
        task.task_number,
        task.work_status.value,
        WorkStatus.BLOCKED.value,
        "system",
        "blocked by dependency",
    )
    service._conn.execute(
        "UPDATE work_tasks SET work_status = ?, updated_at = ? "
        "WHERE project = ? AND task_number = ?",
        (WorkStatus.BLOCKED.value, now, task.project, task.task_number),
    )
    service._conn.commit()


def maybe_unblock(service: "SQLiteWorkService", task_id: str) -> None:
    task = service.get(task_id)
    if task.work_status != WorkStatus.BLOCKED:
        return
    if has_unresolved_blockers(service, task_id):
        return
    now = _now()
    service._record_transition(
        task.project,
        task.task_number,
        WorkStatus.BLOCKED.value,
        WorkStatus.QUEUED.value,
        "system",
        "all blockers resolved",
    )
    service._conn.execute(
        "UPDATE work_tasks SET work_status = ?, updated_at = ? "
        "WHERE project = ? AND task_number = ?",
        (WorkStatus.QUEUED.value, now, task.project, task.task_number),
    )
    service._conn.commit()


def check_auto_unblock(service: "SQLiteWorkService", task_id: str) -> None:
    task = service.get(task_id)
    rows = service._conn.execute(
        "SELECT to_project, to_task_number FROM work_task_dependencies "
        "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
        (task.project, task.task_number, LinkKind.BLOCKS.value),
    ).fetchall()
    for row in rows:
        blocked_id = f"{row['to_project']}/{row['to_task_number']}"
        blocked_task = service.get(blocked_id)
        if blocked_task.work_status != WorkStatus.BLOCKED:
            continue
        if has_unresolved_blockers(service, blocked_id):
            continue
        now = _now()
        service._record_transition(
            blocked_task.project,
            blocked_task.task_number,
            WorkStatus.BLOCKED.value,
            WorkStatus.QUEUED.value,
            "system",
            f"auto-unblocked, blocker {task.task_id} completed",
        )
        service._conn.execute(
            "UPDATE work_tasks SET work_status = ?, updated_at = ? "
            "WHERE project = ? AND task_number = ?",
            (
                WorkStatus.QUEUED.value,
                now,
                blocked_task.project,
                blocked_task.task_number,
            ),
        )
        service._conn.commit()
        service._sync_transition(
            service.get(blocked_id),
            WorkStatus.BLOCKED.value,
            WorkStatus.QUEUED.value,
        )


def on_cancelled(service: "SQLiteWorkService", task_id: str) -> None:
    task = service.get(task_id)
    rows = service._conn.execute(
        "SELECT to_project, to_task_number FROM work_task_dependencies "
        "WHERE from_project = ? AND from_task_number = ? AND kind = ?",
        (task.project, task.task_number, LinkKind.BLOCKS.value),
    ).fetchall()
    for row in rows:
        blocked_id = f"{row['to_project']}/{row['to_task_number']}"
        service.add_context(
            blocked_id,
            "system",
            f"blocker {task.task_id} was cancelled "
            f"— PM must decide whether to unblock or cancel this task.",
        )
