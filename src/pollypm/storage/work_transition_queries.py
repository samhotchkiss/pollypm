"""Read-only work-transition query helpers.

Plugin and presentation layers should not open workspace SQLite files
directly. This module owns the raw connection and schema details for
small projection-style reads that are not yet on a richer work-service
API.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

logger = logging.getLogger(__name__)


_ADVISOR_TRANSITION_SQL = (
    "SELECT t.task_project AS project, t.task_number AS task_number, "
    "       COALESCE(w.title, '') AS title, "
    "       t.from_state AS from_state, t.to_state AS to_state, "
    "       t.actor AS actor, t.created_at AS created_at "
    "FROM work_transitions t "
    "LEFT JOIN work_tasks w "
    "  ON w.project = t.task_project AND w.task_number = t.task_number "
    "WHERE t.task_project = ? AND t.created_at >= ? "
    "ORDER BY t.created_at ASC"
)


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
    except sqlite3.Error as exc:
        logger.debug(
            "work_transition_queries: read-only connect failed for %s: %s",
            db_path,
            exc,
        )
        return None
    apply_workspace_pragmas(conn, readonly=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def advisor_transition_rows(
    db_path: Path,
    *,
    project_key: str,
    since_iso: str,
) -> list[dict[str, Any]]:
    """Return transition rows for advisor change detection."""
    conn = _open_readonly(db_path)
    if conn is None:
        return []
    try:
        try:
            rows = conn.execute(
                _ADVISOR_TRANSITION_SQL,
                (project_key, since_iso),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug(
                "work_transition_queries: advisor transition query failed for %s: %s",
                project_key,
                exc,
            )
            return []
    finally:
        conn.close()
    return [_row_dict(row) for row in rows]


def activity_feed_transition_rows(
    db_path: Path,
    *,
    since_ts: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return recent work-transition rows for activity-feed projection."""
    conn = _open_readonly(db_path)
    if conn is None:
        return []
    try:
        try:
            has_work_transitions = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'work_transitions'"
            ).fetchone()
            if has_work_transitions is None:
                return []

            params: list[Any] = []
            where = ""
            if since_ts is not None:
                where = "WHERE created_at >= ?"
                params.append(since_ts)
            rows = conn.execute(
                "SELECT id, task_project, task_number, from_state, to_state, "
                f"actor, reason, created_at FROM work_transitions {where} "
                f"ORDER BY id DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            logger.debug(
                "work_transition_queries: activity-feed transition query failed for %s: %s",
                db_path,
                exc,
            )
            return []
    finally:
        conn.close()
    return [_row_dict(row) for row in rows]


__all__ = [
    "activity_feed_transition_rows",
    "advisor_transition_rows",
]
