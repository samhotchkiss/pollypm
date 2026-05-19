"""Postgres facade for the ``worktrees`` table (#1737).

This module owns the read/write path for the per-project worktree
inventory that :mod:`pollypm.worktrees` records. Each row tracks a
provisioned worktree directory (path + branch) by project key, lane
(``main`` / ``issue`` / ``task``), and lane-key. Mirrors the three
StateStore methods that used to back the same table on the sqlite
path:

* :func:`upsert_worktree`
* :func:`update_worktree_status`
* :func:`list_worktrees`

All three functions are module-level (no class) and accept an optional
``pool`` kwarg defaulting to :func:`~pollypm.storage.pg_pool.get_rw_pool`
for mutators or :func:`~pollypm.storage.pg_pool.get_ro_pool` for the
reader. Passing a custom pool is the test-harness seam.

Slice K-state-port phase 2b — port of StateStore cluster H.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import WorktreeRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Matches the value StateStore stamps on the sqlite ``created_at`` /
    ``updated_at`` columns so dual-write callers (during the cutover)
    produce indistinguishable rows.
    """
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string.

    pg returns ``timestamptz`` columns as ``datetime`` objects;
    StateStore returns them as strings. This helper normalises the
    pg shape to the sqlite shape so the
    :class:`~pollypm.storage.records.WorktreeRecord` dataclass sees
    the same value either way.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def upsert_worktree(
    *,
    project_key: str,
    lane_kind: str,
    lane_key: str,
    session_name: str | None,
    issue_key: str | None,
    path: str,
    branch: str,
    status: str,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Insert-or-update the worktree row for ``(project, lane, status)``.

    Mirrors the StateStore semantics: rows are deduplicated on
    ``(project_key, lane_kind, lane_key, status)`` so a fresh
    provisioning that lands on an existing active worktree updates
    the existing row in place (path/branch/session_name/issue_key) and
    bumps ``updated_at``. A different ``status`` produces a new row.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM worktrees
            WHERE project_key = %s
              AND lane_kind = %s
              AND lane_key = %s
              AND status = %s
            """,
            (project_key, lane_kind, lane_key, status),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                INSERT INTO worktrees (
                    project_key, lane_kind, lane_key, session_name, issue_key,
                    path, branch, status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_key,
                    lane_kind,
                    lane_key,
                    session_name,
                    issue_key,
                    path,
                    branch,
                    status,
                    now,
                    now,
                ),
            )
        else:
            cur.execute(
                """
                UPDATE worktrees
                SET session_name = %s,
                    issue_key = %s,
                    path = %s,
                    branch = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (session_name, issue_key, path, branch, now, row[0]),
            )


def update_worktree_status(
    project_key: str,
    lane_kind: str,
    lane_key: str,
    status: str,
    *,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Promote the ``active`` row for ``(project, lane)`` to ``status``.

    The sqlite contract: only the currently-active row is affected;
    rows that are already in a terminal status (``closed``, etc) are
    left alone so the historical record is preserved.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE worktrees
            SET status = %s, updated_at = %s
            WHERE project_key = %s
              AND lane_kind = %s
              AND lane_key = %s
              AND status = 'active'
            """,
            (status, now, project_key, lane_kind, lane_key),
        )


def list_worktrees(
    project_key: str | None = None,
    *,
    pool: "ConnectionPool | None" = None,
) -> list[WorktreeRecord]:
    """Return every worktree row, ordered most-recently-updated first.

    Pass ``project_key`` to scope the result to a single project; pass
    ``None`` to return the full inventory.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        if project_key is None:
            cur.execute(
                """
                SELECT project_key, lane_kind, lane_key, session_name, issue_key,
                       path, branch, status, created_at, updated_at
                FROM worktrees
                ORDER BY updated_at DESC
                """
            )
        else:
            cur.execute(
                """
                SELECT project_key, lane_kind, lane_key, session_name, issue_key,
                       path, branch, status, created_at, updated_at
                FROM worktrees
                WHERE project_key = %s
                ORDER BY updated_at DESC
                """,
                (project_key,),
            )
        rows = cur.fetchall()
    return [
        WorktreeRecord(
            project_key=row[0],
            lane_kind=row[1],
            lane_key=row[2],
            session_name=row[3],
            issue_key=row[4],
            path=row[5],
            branch=row[6],
            status=row[7],
            created_at=_stamp_str(row[8]),
            updated_at=_stamp_str(row[9]),
        )
        for row in rows
    ]


__all__ = [
    "list_worktrees",
    "update_worktree_status",
    "upsert_worktree",
]
