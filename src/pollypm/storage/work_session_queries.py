"""Read-only ``work_sessions`` aggregate queries.

Presentation/plugin code should not open workspace SQLite files
directly; this module owns the schema/connection details for small
projection reads against ``work_sessions``.

The aggregate used by the per-project dashboard's Tokens line lives
here so the rendering layer (``cockpit_sections``) no longer has to
``import sqlite3`` or know the table name.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from pollypm.storage.sqlite_pragmas import apply_workspace_pragmas

logger = logging.getLogger(__name__)


def aggregate_project_session_tokens(
    db_path: Path,
    *,
    project_key: str,
) -> tuple[int, int] | None:
    """Return ``(SUM(total_input_tokens), SUM(total_output_tokens))`` for ``project_key``.

    Returns ``None`` if the DB is missing or the query fails (e.g. the
    ``work_sessions`` table does not exist on an old workspace) so the
    Tokens line in the per-project dashboard can degrade to ``(n/a)``
    instead of breaking the render.

    A short ``busy_timeout`` is applied via the standard workspace
    pragmas because the cockpit reader runs alongside JobWorkerPool +
    heartbeat writers on the same DB (#1018).
    """
    try:
        if not db_path.exists():
            return None
    except OSError:
        return None
    # #1652: open read-only via the ``file:<path>?mode=ro`` URI so the
    # render-side aggregate cannot mutate the workspace DB (journal
    # mode, write lock, etc.). Mirrors the doctor probe pattern from
    # #1625 (``doctor_state_probes._connect_readonly``) and the other
    # presentation-side read facades (``morning_briefing_queries``,
    # ``inbox_action_preview``, ``work_task_state``).
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        logger.debug(
            "work_session_queries: connect failed for %s: %s", db_path, exc,
        )
        return None
    try:
        apply_workspace_pragmas(conn, readonly=True)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_input_tokens), 0), "
                "       COALESCE(SUM(total_output_tokens), 0) "
                "FROM work_sessions WHERE task_project = ?",
                (project_key,),
            ).fetchone()
        except sqlite3.Error:
            return None
    finally:
        conn.close()
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


__all__ = ["aggregate_project_session_tokens"]
