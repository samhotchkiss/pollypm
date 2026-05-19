"""Postgres facade for the ``checkpoints`` table (#1737).

This module owns the read/write path for the per-session checkpoint
rows that :mod:`pollypm.checkpoints` records during the heartbeat
cascade and the cockpit's manual-checkpoint flow. Mirrors the two
StateStore methods that used to back the same table on the sqlite
path:

* :func:`record_checkpoint`
* :func:`latest_checkpoint`

Both functions are module-level (no class) and accept an optional
``pool`` kwarg defaulting to :func:`~pollypm.storage.pg_pool.get_rw_pool`
for the writer or :func:`~pollypm.storage.pg_pool.get_ro_pool` for the
reader. Passing a custom pool is the test-harness seam.

Slice K-state-port phase 2b — port of StateStore cluster G.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import CheckpointRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Matches the value StateStore stamps on the sqlite ``created_at``
    column so dual-write callers (during the cutover) produce
    indistinguishable rows.
    """
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string.

    pg returns ``timestamptz`` columns as ``datetime`` objects;
    StateStore returns them as strings. This helper normalises the
    pg shape to the sqlite shape so the
    :class:`~pollypm.storage.records.CheckpointRecord` dataclass sees
    the same value either way.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def record_checkpoint(
    *,
    session_name: str,
    project_key: str,
    level: str,
    json_path: str,
    summary_path: str,
    snapshot_path: str,
    summary_text: str,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Append a checkpoint row.

    Mirrors :meth:`StateStore.record_checkpoint`: each call inserts a
    new row (no upsert — the ``id`` serial gives us a monotonic
    ordering that :func:`latest_checkpoint` uses to find the most
    recent entry).
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    created_at = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO checkpoints (
                session_name, project_key, level, json_path, summary_path,
                snapshot_path, summary_text, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_name,
                project_key,
                level,
                json_path,
                summary_path,
                snapshot_path,
                summary_text,
                created_at,
            ),
        )


def latest_checkpoint(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> CheckpointRecord | None:
    """Return the most recently inserted checkpoint for ``session_name``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, project_key, level, json_path, summary_path,
                   snapshot_path, summary_text, created_at
            FROM checkpoints
            WHERE session_name = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return CheckpointRecord(
        session_name=row[0],
        project_key=row[1],
        level=row[2],
        json_path=row[3],
        summary_path=row[4],
        snapshot_path=row[5],
        summary_text=row[6],
        created_at=_stamp_str(row[7]),
    )


__all__ = [
    "latest_checkpoint",
    "record_checkpoint",
]
