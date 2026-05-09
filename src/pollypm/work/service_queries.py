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
from pollypm.work.role_validation import validate_role_assignments
from pollypm.work.service_support import TaskNotFoundError, ValidationError, _now, _parse_task_id

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


# #1546 — labels that bypass the product-broken gate. The watchdog's
# operator dispatch path creates inbox tasks tagged ``watchdog`` (so
# the cascade can still surface findings even when the workspace is
# product-broken) and the urgent human-handoff path tags its inbox
# rows ``urgent``. Both are out-of-band notifications, not new work
# queueing, so the gate explicitly allows them.
_PRODUCT_BROKEN_BYPASS_LABELS: frozenset[str] = frozenset({
    "watchdog",
    "urgent",
    "notify",
})


# Cache for the product-broken gate. Keyed by (db_path, mtime_ns) so
# the gate is O(1) when the file hasn't been touched since the last
# check. Each ``create_task`` call would otherwise spend the cost of
# opening the workspace state.db (which can be 1+ GB on long-lived
# installs) just to read a single key-value row — observed in the
# #1546 test run pegging an open StateStore handle for every
# ``svc.create`` call across the full pytest suite.
_PRODUCT_STATE_CACHE: dict[tuple[str, int], "Any | None"] = {}


# Sentinel that distinguishes "we checked and the workspace is
# healthy" from "no row yet" in the cache.
_PRODUCT_STATE_SENTINEL_HEALTHY = object()


def _enforce_product_state_gate(*, labels: list[str] | None) -> None:
    """Raise :class:`ProductBrokenError` when workspace product_state=='broken'.

    The lookup is best-effort and cached by (db_path, mtime_ns): a
    missing config / state.db / unified store path falls through
    silently so unit tests that exercise the work-service in isolation
    aren't perturbed. Tasks tagged with any of the bypass labels (the
    watchdog's own dispatch path needs to keep working) skip the gate.

    Reads the workspace_state row via raw sqlite3 (read-only URI) to
    avoid the ``StateStore`` init cost — the gate is in the hot path
    of every ``create_task`` call and a 1+ GB workspace state.db
    can spend tens of ms on the schema-replay + pragma dance the
    StateStore constructor walks. We only need a single SELECT here.
    """
    label_set = {str(label) for label in (labels or []) if label}
    if label_set & _PRODUCT_BROKEN_BYPASS_LABELS:
        return
    try:
        import sqlite3
        from pathlib import Path

        from pollypm.config import DEFAULT_CONFIG_PATH, load_config
        from pollypm.storage.product_state import (
            PRODUCT_STATE_BROKEN,
            PRODUCT_STATE_KEY,
            ProductBrokenError,
            ProductState,
        )
    except Exception:  # noqa: BLE001
        return
    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception:  # noqa: BLE001
        return
    db_path_raw = getattr(getattr(config, "project", None), "state_db", None)
    if db_path_raw is None:
        return
    try:
        db_path = Path(db_path_raw)
    except TypeError:
        return
    if not db_path.exists():
        return
    # mtime-keyed cache — invalidate when the DB file is rewritten so
    # a freshly-set product_state row takes effect on the next
    # heartbeat tick (or sooner if the writer fsyncs).
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return
    cache_key = (str(db_path), mtime_ns)
    cached = _PRODUCT_STATE_CACHE.get(cache_key)
    if cached is _PRODUCT_STATE_SENTINEL_HEALTHY:
        return
    if cached is not None and cached is not _PRODUCT_STATE_SENTINEL_HEALTHY:
        raise ProductBrokenError(cached)

    # Direct sqlite read — cheaper than StateStore for the hot path.
    # Read-only URI mode + immutable=1 lets us peek at a multi-GB DB
    # without any pragma replay or schema-creation overhead.
    raw_value: str | None = None
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro&immutable=1", uri=True,
        )
    except sqlite3.Error:
        # File exists but can't open — record sentinel-healthy so we
        # don't keep retrying.
        _PRODUCT_STATE_CACHE.clear()
        _PRODUCT_STATE_CACHE[cache_key] = _PRODUCT_STATE_SENTINEL_HEALTHY
        return
    try:
        try:
            cur = conn.execute(
                "SELECT value_json FROM workspace_state WHERE key = ?",
                (PRODUCT_STATE_KEY,),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            # Table missing (pre-#1546 DB layout) — treat as healthy.
            _PRODUCT_STATE_CACHE.clear()
            _PRODUCT_STATE_CACHE[cache_key] = _PRODUCT_STATE_SENTINEL_HEALTHY
            return
        if row is not None:
            raw_value = row[0]
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    if raw_value is None:
        _PRODUCT_STATE_CACHE.clear()
        _PRODUCT_STATE_CACHE[cache_key] = _PRODUCT_STATE_SENTINEL_HEALTHY
        return
    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError):
        _PRODUCT_STATE_CACHE.clear()
        _PRODUCT_STATE_CACHE[cache_key] = _PRODUCT_STATE_SENTINEL_HEALTHY
        return
    if not isinstance(payload, dict):
        _PRODUCT_STATE_CACHE.clear()
        _PRODUCT_STATE_CACHE[cache_key] = _PRODUCT_STATE_SENTINEL_HEALTHY
        return
    state = payload.get("state")
    if state != PRODUCT_STATE_BROKEN:
        _PRODUCT_STATE_CACHE.clear()
        _PRODUCT_STATE_CACHE[cache_key] = _PRODUCT_STATE_SENTINEL_HEALTHY
        return
    broken = ProductState(
        state=str(state),
        reason=str(payload.get("reason") or ""),
        set_at=str(payload.get("set_at") or ""),
        set_by=str(payload.get("set_by") or ""),
        forensics_path=str(payload.get("forensics_path") or ""),
        extra=dict(payload.get("extra") or {}),
    )
    _PRODUCT_STATE_CACHE.clear()
    _PRODUCT_STATE_CACHE[cache_key] = broken
    raise ProductBrokenError(broken)


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
    predecessor_task_id: str | None = None,
) -> Task:
    # #1546 — product-broken gate. When the workspace state DB carries
    # ``product_state=broken``, refuse new task queueing with a clear
    # error pointing at the reason + forensics path. The flag is
    # workspace-wide, not per-project, so we look it up via the
    # canonical state DB. The lookup is best-effort — a missing
    # state.db (e.g. tests that exercise the work-service in isolation)
    # falls through to the normal create path so we don't break
    # unrelated callers.
    _enforce_product_state_gate(labels=labels)
    template = service._ensure_flow_in_db(flow_template)

    for role_name, role_def in template.roles.items():
        is_optional = isinstance(role_def, dict) and role_def.get("optional", False)
        if not is_optional and role_name not in roles:
            raise ValidationError(
                f"Required role '{role_name}' not provided. "
                f"Flow '{template.name}' requires: "
                f"{[r for r, d in template.roles.items() if not (isinstance(d, dict) and d.get('optional', False))]}"
            )

    # savethenovel-forensic guard: reject ``user`` / ``human`` and similar
    # non-agent values for roles that drive autonomous-agent nodes (e.g.
    # ``worker=user`` or ``reviewer=user`` would otherwise produce a task
    # whose worker session runs with ``Assignee: user``). Metadata-only
    # roles like ``requester=user`` remain legal — that's the inbox-view
    # convention for marking a task as user-facing.
    validate_role_assignments(template, roles)

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

    # #1398 — normalise predecessor_task_id and let validation surface
    # malformed forms early. ``_parse_task_id`` raises ValidationError
    # on bad input; we deliberately don't verify the predecessor exists
    # in work_tasks here so a caller can re-thread a replan even after
    # the prior attempt has been pruned (e.g. cancelled/cleaned).
    predecessor_normalized: str | None = None
    if predecessor_task_id is not None:
        pred_project, pred_number = _parse_task_id(predecessor_task_id)
        predecessor_normalized = f"{pred_project}/{pred_number}"

    service._conn.execute(
        "INSERT INTO work_tasks "
        "(project, task_number, title, type, labels, work_status, "
        "flow_template_id, flow_template_version, current_node_id, "
        "assignee, priority, requires_human_review, description, "
        "acceptance_criteria, constraints, relevant_files, "
        "roles, external_refs, created_at, created_by, updated_at, "
        "plan_version, predecessor_task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            1,
            predecessor_normalized,
        ),
    )
    service._conn.commit()
    task = service.get(f"{project}/{task_number}")
    # Audit hook (#savethenovel) — record the create so a forensic
    # reader can reconstruct task creation order even if the row is
    # later wiped from work_tasks. Best-effort; emit() never raises.
    try:
        from pollypm.audit import emit as _audit_emit
        from pollypm.audit.log import EVENT_TASK_CREATED

        _audit_emit(
            event=EVENT_TASK_CREATED,
            project=project,
            subject=task.task_id,
            actor=created_by or "system",
            metadata={
                "title": title,
                "type": task_type.value,
                "flow_template": template.name,
                "priority": task_priority.value,
                "requires_human_review": bool(requires_human_review),
            },
            project_path=service._project_path,
        )
        # #1398 — separate event for replan successor links so the
        # heartbeat can render plan-history breadcrumbs without
        # re-parsing every task.created metadata blob.
        if predecessor_normalized is not None:
            from pollypm.audit.log import EVENT_PLAN_SUCCESSOR_CREATED

            _audit_emit(
                event=EVENT_PLAN_SUCCESSOR_CREATED,
                project=project,
                subject=task.task_id,
                actor=created_by or "system",
                metadata={
                    "predecessor": predecessor_normalized,
                    "successor": task.task_id,
                },
                project_path=service._project_path,
            )
    except Exception:  # noqa: BLE001 — audit must never break creates
        pass
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
