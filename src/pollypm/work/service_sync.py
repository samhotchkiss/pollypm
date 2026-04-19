"""Sync-state helpers for the SQLite work service.

Contract:
- Inputs: a ``SQLiteWorkService``, task ids, and sync adapter names.
- Outputs: persisted sync-attempt state and force-sync summaries.
- Side effects: writes ``work_sync_state`` rows and invokes registered
  sync adapters.
- Invariants: sync bookkeeping stays owned by the work service instead
  of leaking into callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pollypm.work.service_support import TaskNotFoundError, _now, _parse_task_id

if TYPE_CHECKING:
    from pollypm.work.sqlite_service import SQLiteWorkService


def record_sync_state(
    service: "SQLiteWorkService",
    project: str,
    task_number: int,
    adapter_name: str,
    *,
    success: bool,
    error: str | None,
) -> None:
    now = _now() if success else None
    service._conn.execute(
        """
        INSERT INTO work_sync_state
            (task_project, task_number, adapter_name,
             last_synced_at, last_error, attempts)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(task_project, task_number, adapter_name)
        DO UPDATE SET
            last_synced_at = COALESCE(excluded.last_synced_at,
                                      work_sync_state.last_synced_at),
            last_error = excluded.last_error,
            attempts = work_sync_state.attempts + 1
        """,
        (project, task_number, adapter_name, now, error),
    )
    service._conn.commit()


def sync_status(
    service: "SQLiteWorkService",
    task_id: str,
) -> dict[str, object]:
    project, task_number = _parse_task_id(task_id)
    row = service._conn.execute(
        "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
        (project, task_number),
    ).fetchone()
    if row is None:
        raise TaskNotFoundError(f"Task '{task_id}' not found.")

    rows = service._conn.execute(
        "SELECT adapter_name, last_synced_at, last_error, attempts "
        "FROM work_sync_state "
        "WHERE task_project = ? AND task_number = ?",
        (project, task_number),
    ).fetchall()
    result: dict[str, object] = {
        row["adapter_name"]: {
            "last_synced_at": row["last_synced_at"],
            "last_error": row["last_error"],
            "attempts": row["attempts"],
        }
        for row in rows
    }

    if service._sync is not None:
        for adapter in service._sync.adapters:
            name = getattr(adapter, "name", None)
            if name and name not in result:
                result[name] = {
                    "last_synced_at": None,
                    "last_error": None,
                    "attempts": 0,
                }

    return result


def trigger_sync(
    service: "SQLiteWorkService",
    *,
    task_id: str | None = None,
    adapter: str | None = None,
) -> dict[str, object]:
    summary: dict[str, object] = {"synced": 0, "errors": {}}

    if task_id is None:
        rows = service._conn.execute(
            "SELECT project, task_number FROM work_tasks "
            "ORDER BY project, task_number",
        ).fetchall()
        task_ids = [f"{row['project']}/{row['task_number']}" for row in rows]
    else:
        project, task_number = _parse_task_id(task_id)
        row = service._conn.execute(
            "SELECT 1 FROM work_tasks WHERE project = ? AND task_number = ?",
            (project, task_number),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task '{task_id}' not found.")
        task_ids = [task_id]

    if service._sync is None:
        return summary

    adapters = [
        item for item in service._sync.adapters
        if adapter is None or getattr(item, "name", None) == adapter
    ]
    if not adapters:
        return summary

    errors: dict[str, list[str]] = {}
    synced = 0

    for tid in task_ids:
        try:
            task = service.get(tid)
        except TaskNotFoundError:
            continue

        project, task_number = _parse_task_id(tid)
        for current in adapters:
            name = getattr(current, "name", "unknown")
            err: str | None = None
            try:
                current.on_create(task)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                errors.setdefault(name, []).append(tid)

            record_sync_state(
                service,
                project,
                task_number,
                name,
                success=(err is None),
                error=err,
            )
            if err is None:
                synced += 1

    summary["synced"] = synced
    summary["errors"] = errors
    return summary
