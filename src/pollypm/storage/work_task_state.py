"""Read-only work-task state probes.

This module owns the raw SQLite reads used by rail and recurring
maintenance code. Callers above storage should go through
``pollypm.work.task_state`` so UI/plugin modules do not know table
names or connection details.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

logger = logging.getLogger(__name__)


def _candidate_paths(
    project_path: Path,
    *,
    workspace_root: Path | None = None,
) -> list[Path]:
    paths = [project_path / ".pollypm" / "state.db"]
    if workspace_root is not None:
        paths.append(workspace_root / ".pollypm" / "state.db")
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _connect_readonly(db_path: Path) -> sqlite3.Connection | None:
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("work_task_state: read-only connect failed for %s: %s", db_path, exc)
        return None
    apply_workspace_pragmas(conn, readonly=True)
    return conn


def task_status_probe(
    *,
    project_key: str,
    task_number: int,
    project_path: Path,
    workspace_root: Path | None = None,
) -> tuple[bool, str | None]:
    """Return ``(found_any_db, status)`` for a task lookup.

    ``status`` is ``None`` when no candidate DB contains the task row.
    Transient SQLite errors are treated as misses for that DB so callers
    can keep their existing best-effort behavior.
    """
    found_any_db = False
    for db_path in _candidate_paths(project_path, workspace_root=workspace_root):
        try:
            if not db_path.exists():
                continue
        except OSError:
            continue
        found_any_db = True
        conn = _connect_readonly(db_path)
        if conn is None:
            continue
        try:
            try:
                row = conn.execute(
                    "SELECT work_status FROM work_tasks "
                    "WHERE project = ? AND task_number = ?",
                    (project_key, int(task_number)),
                ).fetchone()
            except sqlite3.Error:
                continue
        finally:
            conn.close()
        if row is None:
            continue
        status = row[0]
        return found_any_db, status if isinstance(status, str) else None
    return found_any_db, None


def task_numbers_with_statuses(
    *,
    project_key: str,
    project_path: Path,
    statuses: Iterable[str],
    workspace_root: Path | None = None,
) -> list[int]:
    """Return sorted task numbers whose ``work_status`` is in ``statuses``."""
    status_values = tuple(str(status) for status in statuses)
    if not status_values:
        return []
    placeholders = ", ".join("?" for _ in status_values)
    for db_path in _candidate_paths(project_path, workspace_root=workspace_root):
        conn = _connect_readonly(db_path)
        if conn is None:
            continue
        try:
            try:
                rows = conn.execute(
                    "SELECT task_number FROM work_tasks "
                    "WHERE project = ? "
                    f"AND work_status IN ({placeholders}) "
                    "ORDER BY task_number ASC",
                    (project_key, *status_values),
                ).fetchall()
            except sqlite3.Error:
                continue
        finally:
            conn.close()
        if not rows:
            continue
        out: list[int] = []
        for row in rows:
            try:
                out.append(int(row[0]))
            except (TypeError, ValueError):
                continue
        return out
    return []


def project_activity_probe(
    *,
    project_key: str,
    project_path: Path,
    cutoff_iso: str,
    workspace_root: Path | None = None,
) -> tuple[bool, bool]:
    """Return ``(is_active, has_working_task)`` from work-task rows."""
    for db_path in _candidate_paths(project_path, workspace_root=workspace_root):
        conn = _connect_readonly(db_path)
        if conn is None:
            continue
        try:
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT "
                    "  SUM(CASE WHEN work_status = 'in_progress' "
                    "           THEN 1 ELSE 0 END) AS working_count, "
                    "  MAX(updated_at) AS max_updated "
                    "FROM work_tasks WHERE project = ?",
                    (project_key,),
                ).fetchone()
            except sqlite3.Error:
                continue
        finally:
            conn.close()
        if row is None:
            continue
        working_count = row["working_count"] or 0
        max_updated = row["max_updated"] or ""
        has_working_task = working_count > 0
        is_active = bool(has_working_task or (max_updated and max_updated >= cutoff_iso))
        if is_active or has_working_task:
            return is_active, has_working_task
    return False, False


def blocked_since_stamp(
    conn: sqlite3.Connection,
    *,
    project_key: str,
    task_number: int,
    blocked_status: str,
) -> object | None:
    """Return the raw timestamp a task most recently entered blocked."""
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
    """Walk ``blocks`` dependencies and return blocker status values."""
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
