"""Read-only SQLite queries for morning briefing handlers."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


_TRANSITION_SQL = (
    "SELECT t.task_project AS project, t.task_number AS task_number, "
    "       COALESCE(w.title, '') AS title, "
    "       t.from_state AS from_state, t.to_state AS to_state, "
    "       t.actor AS actor, t.created_at AS created_at "
    "FROM work_transitions t "
    "LEFT JOIN work_tasks w "
    "  ON w.project = t.task_project AND w.task_number = t.task_number "
    "WHERE t.task_project = ? "
    "  AND t.created_at >= ? AND t.created_at < ? "
    "ORDER BY t.created_at ASC"
)


def _open_readonly(db_path: Path) -> sqlite3.Connection | None:
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("briefing: ro connect failed for %s: %s", db_path, exc)
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def transition_rows(
    db_paths: Iterable[Path],
    *,
    project_key: str,
    since_iso: str,
    until_iso: str,
) -> list[dict[str, Any]]:
    """Return work transition rows for ``project_key`` in a time window."""
    out: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int, str]] = set()
    for db_path in db_paths:
        conn = _open_readonly(db_path)
        if conn is None:
            continue
        try:
            try:
                rows = conn.execute(
                    _TRANSITION_SQL, (project_key, since_iso, until_iso),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.debug("briefing: transitions SQL failed for %s: %s", project_key, exc)
                continue
        finally:
            conn.close()

        for row in rows:
            proj = str(row["project"] or "")
            try:
                num = int(row["task_number"])
            except (TypeError, ValueError):
                continue
            ts = str(row["created_at"] or "")
            dedupe_key = (proj, num, ts)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            out.append(_row_dict(row))
    return out


def downtime_artifact_rows(
    db_paths: Iterable[Path],
    *,
    project_key: str,
    since_iso: str,
    until_iso: str,
) -> list[dict[str, Any]]:
    """Return downtime tasks that reached awaiting approval in a window."""
    out: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int, str]] = set()
    for db_path in db_paths:
        conn = _open_readonly(db_path)
        if conn is None:
            continue
        try:
            try:
                rows = conn.execute(
                    "SELECT t.task_project AS project, t.task_number AS task_number, "
                    "       COALESCE(w.title, '') AS title, t.created_at AS created_at, "
                    "       COALESCE(w.labels, '[]') AS labels "
                    "FROM work_transitions t "
                    "LEFT JOIN work_tasks w "
                    "  ON w.project = t.task_project AND w.task_number = t.task_number "
                    "WHERE t.task_project = ? "
                    "  AND t.to_state = 'awaiting_approval' "
                    "  AND t.created_at >= ? AND t.created_at < ? "
                    "ORDER BY t.created_at ASC",
                    (project_key, since_iso, until_iso),
                ).fetchall()
            except sqlite3.Error:
                continue
        finally:
            conn.close()

        for row in rows:
            labels_raw = str(row["labels"] or "[]")
            if "downtime" not in labels_raw.lower():
                continue
            proj = str(row["project"] or "")
            try:
                num = int(row["task_number"])
            except (TypeError, ValueError):
                continue
            ts = str(row["created_at"] or "")
            dedupe_key = (proj, num, ts)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            out.append(_row_dict(row))
    return out


def priority_task_rows(
    db_paths: Iterable[Path],
    *,
    project_key: str,
    open_statuses: tuple[str, ...],
    limit: int,
) -> list[dict[str, Any]]:
    """Return candidate priority task rows for one project."""
    out: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, int]] = set()
    for db_path in db_paths:
        conn = _open_readonly(db_path)
        if conn is None:
            continue
        try:
            try:
                rows = conn.execute(
                    "SELECT project, task_number, title, priority, work_status, "
                    "       COALESCE(assignee, '') AS assignee, created_at, updated_at "
                    "FROM work_tasks "
                    "WHERE project = ? AND work_status IN (?, ?, ?, ?) "
                    "ORDER BY "
                    "  CASE priority "
                    "    WHEN 'critical' THEN 0 "
                    "    WHEN 'high' THEN 1 "
                    "    WHEN 'normal' THEN 2 "
                    "    WHEN 'low' THEN 3 ELSE 4 END ASC, "
                    "  updated_at ASC "
                    "LIMIT ?",
                    (project_key, *open_statuses, max(1, limit) * 4),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.debug("briefing: top-tasks SQL failed for %s: %s", project_key, exc)
                continue
        finally:
            conn.close()

        for row in rows:
            proj = str(row["project"] or "")
            try:
                num = int(row["task_number"])
            except (TypeError, ValueError):
                continue
            dedupe_key = (proj, num)
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            out.append(_row_dict(row))
    return out


def blocker_rows(db_paths: Iterable[Path], *, project_key: str) -> list[dict[str, Any]]:
    """Return blocked tasks with their blocker references."""
    results: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, int]] = set()
    for db_path in db_paths:
        conn = _open_readonly(db_path)
        if conn is None:
            continue
        try:
            try:
                rows = conn.execute(
                    "SELECT project, task_number, title "
                    "FROM work_tasks "
                    "WHERE project = ? AND work_status = 'blocked' "
                    "ORDER BY updated_at ASC",
                    (project_key,),
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                proj = str(row["project"] or "")
                try:
                    num = int(row["task_number"])
                except (TypeError, ValueError):
                    continue
                dedupe_key = (proj, num)
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                try:
                    dep_rows = conn.execute(
                        "SELECT d.to_project AS to_project, d.to_task_number AS to_task_number, "
                        "       COALESCE(t.work_status, '') AS blocker_status "
                        "FROM work_task_dependencies d "
                        "LEFT JOIN work_tasks t "
                        "  ON t.project = d.to_project AND t.task_number = d.to_task_number "
                        "WHERE d.from_project = ? AND d.from_task_number = ? "
                        "  AND d.kind = 'blocks' ",
                        (proj, num),
                    ).fetchall()
                except sqlite3.Error:
                    dep_rows = []
                blocked_by: list[str] = []
                unresolved: list[str] = []
                for dep in dep_rows:
                    blocker_id = f"{dep['to_project']}/{dep['to_task_number']}"
                    blocked_by.append(blocker_id)
                    status = str(dep["blocker_status"] or "").lower()
                    if status and status not in ("done", "cancelled"):
                        unresolved.append(blocker_id)
                item = _row_dict(row)
                item["blocked_by"] = blocked_by
                item["unresolved_blockers"] = unresolved
                results.append(item)
        finally:
            conn.close()
    return results


__all__ = [
    "blocker_rows",
    "downtime_artifact_rows",
    "priority_task_rows",
    "transition_rows",
]
