"""Read-only work-task state probes (Postgres-only).

This module owns the raw reads used by rail and recurring maintenance
code. Callers above storage should go through ``pollypm.work.task_state``
so UI/plugin modules do not know table names or connection details.

Following Slice K-state-callers-port (#1737), the probes target the
Postgres RO pool. The ``db_path`` / ``project_path`` / ``workspace_root``
parameters remain in the signatures for caller compatibility but are
unused. The ``blocked_since_stamp`` and ``blocker_chain_statuses``
helpers still accept a DB-API ``Connection`` because they are called
from inside work-service code that already owns a connection — they are
not backend-dispatched.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _pg_ro_conn(config: "PollyPMConfig | None" = None):
    """Borrow an RO connection from the pool or return ``None``."""
    try:
        from pollypm.storage.pg_pool import get_ro_pool
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_task_state: pg_pool import failed: %s", exc)
        return None
    try:
        return get_ro_pool(config).connection()
    except Exception as exc:  # noqa: BLE001
        logger.debug("work_task_state: get_ro_pool failed: %s", exc)
        return None


def task_status_probe(
    *,
    project_key: str,
    task_number: int,
    project_path: Path,
    workspace_root: Path | None = None,
    config: "PollyPMConfig | None" = None,
) -> tuple[bool, str | None]:
    """Return ``(found_any_db, status)`` for a task lookup.

    ``status`` is ``None`` when no row is found. Pool / query errors
    return ``(False, None)``. ``project_path`` / ``workspace_root`` are
    unused on the pg backend.
    """
    del project_path, workspace_root  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return False, None
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT work_status FROM work_tasks "
                    "WHERE project = %s AND task_number = %s",
                    (project_key, int(task_number)),
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                return True, None
            if row is None:
                return True, None
            status = row[0]
            return True, status if isinstance(status, str) else None
    except Exception:  # noqa: BLE001
        return False, None


def bump_reap_count_and_demote(
    *,
    project_key: str,
    task_number: int,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Atomically demote a reaped task back to ``queued`` and bump ``reap_count``.

    Called by :func:`pollypm.work.worker_marker_reaper._classify_marker`
    when a fresh-launch marker is reaped because the tmux window has
    vanished while the task is still non-terminal (#1999). The single
    UPDATE bumps ``reap_count`` and demotes the task in one round-trip,
    returning the post-increment count so the caller can decide whether
    to escalate (3rd+ reap → inbox notify).

    The update is conservative: it only fires when the task is still in
    a non-terminal status (``in_progress`` / ``review`` / ``queued`` /
    ``rework``). If a competing actor already cancelled or completed the
    task between marker classification and this call, the row guard
    prevents us from clobbering the terminal state — we return ``None``
    in that case and the reaper skips the escalation.

    Returns
    -------
    int | None
        The post-increment ``reap_count`` value (>= 1) when the demote
        landed. ``None`` when the row could not be found, the row was
        already terminal, or the pool / query failed.
    """
    try:
        from pollypm.storage.pg_pool import get_rw_pool
    except Exception:  # noqa: BLE001
        logger.debug("bump_reap_count_and_demote: pg_pool import failed", exc_info=True)
        return None
    try:
        rw_ctx = get_rw_pool(config).connection()
    except Exception:  # noqa: BLE001
        logger.debug("bump_reap_count_and_demote: get_rw_pool failed", exc_info=True)
        return None

    try:
        with rw_ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "UPDATE work_tasks "
                    "SET reap_count = reap_count + 1, "
                    "    work_status = 'queued', "
                    "    assignee = NULL, "
                    "    updated_at = now() "
                    "WHERE project = %s AND task_number = %s "
                    "  AND work_status NOT IN ('done', 'cancelled', 'abandoned') "
                    "RETURNING reap_count",
                    (project_key, int(task_number)),
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "bump_reap_count_and_demote: UPDATE failed for %s/%s",
                    project_key, task_number, exc_info=True,
                )
                conn.rollback()
                return None
            if row is None:
                # No row matched — either the task is gone or it's
                # already terminal. Either way, no demote, no escalate.
                conn.rollback()
                return None
            try:
                count = int(row[0])
            except (TypeError, ValueError):
                conn.rollback()
                return None
            conn.commit()
            return count
    except Exception:  # noqa: BLE001
        logger.debug(
            "bump_reap_count_and_demote: connection failed for %s/%s",
            project_key, task_number, exc_info=True,
        )
        return None


def task_numbers_with_statuses(
    *,
    project_key: str,
    project_path: Path,
    statuses: Iterable[str],
    workspace_root: Path | None = None,
    config: "PollyPMConfig | None" = None,
) -> list[int]:
    """Return sorted task numbers whose ``work_status`` is in ``statuses``."""
    del project_path, workspace_root  # unused on pg backend
    status_values = tuple(str(status) for status in statuses)
    if not status_values:
        return []
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return []
    placeholders_pg = ", ".join("%s" for _ in status_values)
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT task_number FROM work_tasks "
                    "WHERE project = %s "
                    f"AND work_status IN ({placeholders_pg}) "
                    "ORDER BY task_number ASC",
                    (project_key, *status_values),
                )
                rows = cur.fetchall()
            except Exception:  # noqa: BLE001
                return []
    except Exception:  # noqa: BLE001
        return []
    out: list[int] = []
    for row in rows:
        try:
            out.append(int(row[0]))
        except (TypeError, ValueError):
            continue
    return out


def project_task_total_fast(
    db_path: Path,
    *,
    project_key: str,
    connect_timeout: float = 0.05,
    busy_timeout_ms: int = 50,
    config: "PollyPMConfig | None" = None,
) -> int | None:
    """Return the work-task count for ``project_key`` quickly.

    ``None`` signals "pool unreachable" (settings screen renders a
    ``busy`` indicator); otherwise a count is returned (``0`` on
    per-query failure). ``db_path`` / ``connect_timeout`` /
    ``busy_timeout_ms`` are unused on the pg backend but kept for
    caller compatibility.
    """
    del db_path, connect_timeout, busy_timeout_ms  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return None
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM work_tasks WHERE project = %s",
                    (project_key,),
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                return 0
            if row is None:
                return 0
            try:
                return int(row[0] or 0)
            except (TypeError, ValueError):
                return 0
    except Exception:  # noqa: BLE001
        return 0


def has_work_task_rows(
    db_path: Path,
    *,
    project_key: str | None = None,
    config: "PollyPMConfig | None" = None,
) -> bool:
    """Return whether ``work_tasks`` has rows, optionally scoped by project."""
    del db_path  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return False
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'work_tasks'"
                )
                if cur.fetchone() is None:
                    return False
                if project_key:
                    cur.execute(
                        "SELECT 1 FROM work_tasks WHERE project = %s LIMIT 1",
                        (project_key,),
                    )
                else:
                    cur.execute("SELECT 1 FROM work_tasks LIMIT 1")
                return cur.fetchone() is not None
            except Exception:  # noqa: BLE001
                return False
    except Exception:  # noqa: BLE001
        return False


def project_activity_probe(
    *,
    project_key: str,
    project_path: Path,
    cutoff_iso: str,
    workspace_root: Path | None = None,
    config: "PollyPMConfig | None" = None,
) -> tuple[bool, bool]:
    """Return ``(is_active, has_working_task)`` from work-task rows."""
    del project_path, workspace_root  # unused on pg backend
    ctx = _pg_ro_conn(config)
    if ctx is None:
        return False, False
    try:
        with ctx as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT "
                    "  SUM(CASE WHEN work_status = 'in_progress' "
                    "           THEN 1 ELSE 0 END) AS working_count, "
                    "  MAX(updated_at::text) AS max_updated "
                    "FROM work_tasks WHERE project = %s",
                    (project_key,),
                )
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                return False, False
            if row is None:
                return False, False
            working_count = int(row[0] or 0)
            max_updated = str(row[1] or "")
            has_working_task = working_count > 0
            is_active = bool(
                has_working_task
                or (max_updated and max_updated >= cutoff_iso)
            )
            return is_active, has_working_task
    except Exception:  # noqa: BLE001
        return False, False


def blocked_since_stamp(
    conn: sqlite3.Connection,
    *,
    project_key: str,
    task_number: int,
    blocked_status: str,
) -> object | None:
    """Return the raw timestamp a task most recently entered blocked.

    Operates on a caller-supplied DB-API connection (work-service path).
    """
    try:
        row = conn.execute(
            "SELECT created_at FROM work_transitions "
            "WHERE task_project = ? AND task_number = ? AND to_state = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_key, int(task_number), blocked_status),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        return row["created_at"] if hasattr(row, "keys") else row[0]
    try:
        task_row = conn.execute(
            "SELECT updated_at, created_at FROM work_tasks "
            "WHERE project = ? AND task_number = ?",
            (project_key, int(task_number)),
        ).fetchone()
    except sqlite3.Error:
        task_row = None
    if task_row is None:
        return None
    if hasattr(task_row, "keys"):
        return task_row["updated_at"] or task_row["created_at"]
    return task_row[0] or task_row[1]


def blocker_chain_statuses(
    conn: sqlite3.Connection,
    *,
    project_key: str,
    task_number: int,
) -> tuple[set[tuple[str, int]], dict[tuple[str, int], str]]:
    """Walk ``blocks`` dependencies and return blocker status values.

    Operates on a caller-supplied DB-API connection (work-service path).
    """
    visited: set[tuple[str, int]] = set()
    status_by_key: dict[tuple[str, int], str] = {}
    stack: list[tuple[str, int]] = [(project_key, int(task_number))]

    while stack:
        cur_project, cur_number = stack.pop()
        try:
            rows = conn.execute(
                "SELECT from_project, from_task_number FROM work_task_dependencies "
                "WHERE to_project = ? AND to_task_number = ? AND kind = 'blocks'",
                (cur_project, cur_number),
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            from_project = row["from_project"] if hasattr(row, "keys") else row[0]
            from_task_number = row["from_task_number"] if hasattr(row, "keys") else row[1]
            key = (str(from_project), int(from_task_number))
            if key in visited:
                continue
            visited.add(key)
            try:
                status_row = conn.execute(
                    "SELECT work_status FROM work_tasks "
                    "WHERE project = ? AND task_number = ?",
                    key,
                ).fetchone()
            except sqlite3.Error:
                status_row = None
            if status_row is None:
                status_by_key[key] = ""
            elif hasattr(status_row, "keys"):
                status_by_key[key] = str(status_row["work_status"] or "")
            else:
                status_by_key[key] = str(status_row[0] or "")
            stack.append(key)

    return visited, status_by_key
