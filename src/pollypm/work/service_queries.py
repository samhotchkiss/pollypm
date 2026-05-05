"""Task persistence and query helpers for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService`` plus task fields and query filters.
- Outputs: typed ``Task`` records and query result lists.
- Side effects: persists task rows and dispatches sync create/update
  hooks owned by the service.
- Invariants: task CRUD stays behind the service boundary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pollypm.work.models import Priority, Task, TaskType, WorkStatus
from pollypm.work.service_support import TaskNotFoundError, ValidationError, _now, _parse_task_id

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


def create_task(
    service: "SQLiteWorkService",
    *,
    title: str,
    description: str = "",
    type: str = "task",
    flow_template: str = "chat",
    roles: dict[str, str],
    project: str,
    priority: str = "normal",
    created_by: str = "system",
    acceptance_criteria: str | None = None,
    constraints: str | None = None,
    relevant_files: list[str] | None = None,
    labels: list[str] | None = None,
    requires_human_review: bool = False,
) -> Task:
    template = service._ensure_flow_in_db(flow_template)

    for role_name, role_def in template.roles.items():
        is_optional = isinstance(role_def, dict) and role_def.get("optional", False)
        if not is_optional and role_name not in roles:
            raise ValidationError(
                f"Required role '{role_name}' not provided. "
                f"Flow '{template.name}' requires: "
                f"{[r for r, d in template.roles.items() if not (isinstance(d, dict) and d.get('optional', False))]}"
            )

    try:
        task_type = TaskType(type)
    except ValueError as exc:
        raise ValidationError(f"Invalid task type '{type}'.") from exc

    try:
        task_priority = Priority(priority)
    except ValueError as exc:
        raise ValidationError(f"Invalid priority '{priority}'.") from exc

    now = _now()
    row = service._conn.execute(
        "SELECT COALESCE(MAX(task_number), 0) AS max_num "
        "FROM work_tasks WHERE project = ?",
        (project,),
    ).fetchone()
    task_number = row["max_num"] + 1

    service._conn.execute(
        "INSERT INTO work_tasks "
        "(project, task_number, title, type, labels, work_status, "
        "flow_template_id, flow_template_version, current_node_id, "
        "assignee, priority, requires_human_review, description, "
        "acceptance_criteria, constraints, relevant_files, "
        "roles, external_refs, created_at, created_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project,
            task_number,
            title,
            task_type.value,
            json.dumps(labels or []),
            WorkStatus.DRAFT.value,
            template.name,
            template.version,
            None,
            None,
            task_priority.value,
            int(requires_human_review),
            description,
            acceptance_criteria,
            constraints,
            json.dumps(relevant_files or []),
            json.dumps(roles),
            json.dumps({}),
            now,
            created_by,
            now,
        ),
    )
    service._conn.commit()
    task = service.get(f"{project}/{task_number}")
    if service._sync:
        external_refs_before_sync = dict(task.external_refs)
        service._sync.on_create(task)
        changed_refs = {
            key: value
            for key, value in task.external_refs.items()
            if external_refs_before_sync.get(key) != value
        }
        for key, value in changed_refs.items():
            service.set_external_ref(task.task_id, key, value)
        if changed_refs:
            task = service.get(task.task_id)
    return task


def get_task(service: "SQLiteWorkService", task_id: str) -> Task:
    project, task_number = _parse_task_id(task_id)
    row = service._conn.execute(
        "SELECT * FROM work_tasks WHERE project = ? AND task_number = ?",
        (project, task_number),
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"Task '{task_id}' not found.")
    return service._row_to_task(row)


def list_tasks(
    service: "SQLiteWorkService",
    *,
    work_status: str | None = None,
    owner: str | None = None,
    project: str | None = None,
    assignee: str | None = None,
    blocked: bool | None = None,
    type: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Task]:
    clauses: list[str] = []
    params: list[object] = []
    if work_status is not None:
        clauses.append("work_status = ?")
        params.append(work_status)
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    if assignee is not None:
        clauses.append("assignee = ?")
        params.append(assignee)
    if type is not None:
        clauses.append("type = ?")
        params.append(type)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM work_tasks{where} ORDER BY project, task_number"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    if offset is not None:
        sql += f" OFFSET {int(offset)}"

    rows = service._conn.execute(sql, params).fetchall()
    token_sums = service._load_task_token_sums_bulk(project=project)
    tasks = [service._row_to_task(row, token_sums=token_sums) for row in rows]
    if owner is not None:
        tasks = [task for task in tasks if service.derive_owner(task) == owner]
    if blocked is not None:
        tasks = [task for task in tasks if task.blocked == blocked]
    return tasks


def list_nonterminal_tasks(
    service: "SQLiteWorkService",
    *,
    project: str | None = None,
) -> list[Task]:
    clauses = ["work_status NOT IN (?, ?)"]
    params: list[object] = [WorkStatus.DONE.value, WorkStatus.CANCELLED.value]
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    rows = service._conn.execute(
        f"SELECT * FROM work_tasks WHERE {' AND '.join(clauses)} "
        "ORDER BY project, task_number",
        params,
    ).fetchall()
    token_sums = service._load_task_token_sums_bulk(project=project)
    tasks: list[Task] = []
    for row in rows:
        try:
            raw_labels = json.loads(row["labels"] or "[]")
        except (TypeError, ValueError):
            raw_labels = []
        labels = set(raw_labels if isinstance(raw_labels, list) else [])
        tasks.append(
            service._row_to_task(
                row,
                token_sums=token_sums,
                include_history="plan_review" in labels,
            )
        )
    return tasks


def update_task(service: "SQLiteWorkService", task_id: str, **fields: object) -> Task:
    if "work_status" in fields:
        raise ValidationError(
            "Cannot change work_status via update(). "
            "Use lifecycle methods (queue, claim, cancel, etc.)."
        )
    if "flow_template" in fields or "flow_template_id" in fields:
        raise ValidationError("Cannot change flow_template after creation.")

    project, task_number = _parse_task_id(task_id)
    existing = service._conn.execute(
        "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
        (project, task_number),
    ).fetchone()
    if existing is None:
        raise TaskNotFoundError(f"Task '{task_id}' not found.")

    allowed = {
        "title": "title",
        "description": "description",
        "priority": "priority",
        "labels": "labels",
        "roles": "roles",
        "acceptance_criteria": "acceptance_criteria",
        "constraints": "constraints",
        "relevant_files": "relevant_files",
    }

    set_clauses: list[str] = []
    params: list[object] = []
    for key, value in fields.items():
        column = allowed.get(key)
        if column is None:
            raise ValidationError(f"Field '{key}' is not updatable.")
        if key in {"labels", "relevant_files", "roles"}:
            value = json.dumps(value)
        set_clauses.append(f"{column} = ?")
        params.append(value)

    if not set_clauses:
        return service.get(task_id)

    set_clauses.append("updated_at = ?")
    params.append(_now())
    params.extend([project, task_number])
    service._conn.execute(
        f"UPDATE work_tasks SET {', '.join(set_clauses)} "
        "WHERE project = ? AND task_number = ?",
        params,
    )
    service._conn.commit()
    task = service.get(task_id)
    if service._sync:
        service._sync.on_update(task, list(fields.keys()))
    return task


def _unresolved_blocked_task_keys(
    service: "SQLiteWorkService",
    *,
    project: str | None = None,
) -> set[tuple[str, int]]:
    clauses = [
        "d.kind = ?",
        "t.work_status NOT IN (?, ?)",
    ]
    params: list[object] = [
        "blocks",
        WorkStatus.DONE.value,
        WorkStatus.CANCELLED.value,
    ]
    if project is not None:
        clauses.append("d.to_project = ?")
        params.append(project)
    rows = service._conn.execute(
        "SELECT DISTINCT d.to_project, d.to_task_number "
        "FROM work_task_dependencies d "
        "JOIN work_tasks t "
        "  ON t.project = d.from_project "
        " AND t.task_number = d.from_task_number "
        f"WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    return {(row["to_project"], row["to_task_number"]) for row in rows}


def next_task(
    service: "SQLiteWorkService",
    *,
    agent: str | None = None,
    project: str | None = None,
) -> Task | None:
    blocked_keys = _unresolved_blocked_task_keys(service, project=project)
    clauses = ["t.work_status = ?"]
    params: list[object] = [WorkStatus.QUEUED.value]
    if project is not None:
        clauses.append("t.project = ?")
        params.append(project)
    where = " AND ".join(clauses)
    sql = (
        "SELECT t.* FROM work_tasks t "
        f"WHERE {where} "
        "ORDER BY "
        "CASE t.priority "
        "  WHEN 'critical' THEN 0 "
        "  WHEN 'high' THEN 1 "
        "  WHEN 'normal' THEN 2 "
        "  WHEN 'low' THEN 3 "
        "  ELSE 4 "
        "END, "
        "t.created_at ASC"
    )
    rows = service._conn.execute(sql, params).fetchall()
    for row in rows:
        task_key = (row["project"], row["task_number"])
        if task_key in blocked_keys:
            continue
        if agent is not None:
            # ``roles`` is producer-controlled and always serialised as a
            # dict, but a hand-edited or legacy DB row could land an
            # empty string / null / non-dict shape. Defer to the shared
            # ``_safe_json_dict`` helper so the corrupt-payload defense
            # stays consistent with ``_row_to_task`` / ``_get_flow``
            # (cycles 107-113).
            from pollypm.work.sqlite_service import _safe_json_dict
            if _safe_json_dict(row["roles"]).get("worker") != agent:
                continue
        return service._row_to_task(row)
    return None


def my_tasks(service: "SQLiteWorkService", agent: str) -> list[Task]:
    rows = service._conn.execute(
        "SELECT * FROM work_tasks "
        "WHERE current_node_id IS NOT NULL AND assignee = ? "
        "ORDER BY project, task_number",
        (agent,),
    ).fetchall()
    return [service._row_to_task(row) for row in rows]


def state_counts(service: "SQLiteWorkService", project: str | None = None) -> dict[str, int]:
    counts = {status.value: 0 for status in WorkStatus}
    clauses: list[str] = []
    params: list[object] = []
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = service._conn.execute(
        f"SELECT work_status, COUNT(*) as cnt FROM work_tasks{where} GROUP BY work_status",
        params,
    ).fetchall()
    for row in rows:
        counts[row["work_status"]] = row["cnt"]
    return counts


def blocked_tasks(service: "SQLiteWorkService", project: str | None = None) -> list[Task]:
    clauses = ["work_status = ?"]
    params: list[object] = [WorkStatus.BLOCKED.value]
    if project is not None:
        clauses.append("project = ?")
        params.append(project)
    rows = service._conn.execute(
        f"SELECT * FROM work_tasks WHERE {' AND '.join(clauses)} ORDER BY project, task_number",
        params,
    ).fetchall()
    return [service._row_to_task(row) for row in rows]
