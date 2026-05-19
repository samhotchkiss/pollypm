"""Postgres facade for the ``heartbeats`` table (#1737).

This module owns the read/write path for cluster C of the StateStore
port plan: the per-session heartbeat ledger that the supervisor cascade
writes after every sweep and that the cockpit / rail / health probes
read for "is this session alive?" decisions.

Public API:

* :func:`record_heartbeat` — INSERT a fresh heartbeat row
* :func:`latest_heartbeat` — SELECT the most-recent row for a session
* :func:`recent_heartbeats` — SELECT the N most-recent rows
* :func:`last_heartbeat_at` — SELECT the most-recent heartbeat event
  timestamp (event-based, not the heartbeats table itself — sweep
  observations land in ``messages`` with ``type='event'`` per #349)

All functions accept an optional ``pool`` kwarg. The default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the writer and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the readers; passing a
custom pool is the test-harness seam.

Slice K-state-port phase 2d — port of StateStore cluster C.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import HeartbeatRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string (pg datetime → sqlite-style str)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def record_heartbeat(
    *,
    session_name: str,
    tmux_window: str,
    pane_id: str,
    pane_command: str,
    pane_dead: bool,
    log_bytes: int,
    snapshot_path: str,
    snapshot_hash: str,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Append a heartbeat row.

    Mirrors :meth:`StateStore.record_heartbeat`: each call inserts a new
    row (no upsert — the ``id`` serial gives a monotonic ordering that
    :func:`latest_heartbeat` and :func:`recent_heartbeats` use to find
    the most recent observations).
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO heartbeats (
                session_name, tmux_window, pane_id, pane_command, pane_dead,
                log_bytes, snapshot_path, snapshot_hash, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session_name,
                tmux_window,
                pane_id,
                pane_command,
                bool(pane_dead),
                int(log_bytes),
                snapshot_path,
                snapshot_hash,
                now,
            ),
        )


def latest_heartbeat(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> HeartbeatRecord | None:
    """Return the most-recent heartbeat row for ``session_name``, or ``None``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, tmux_window, pane_id, pane_command, pane_dead,
                   log_bytes, snapshot_path, snapshot_hash, created_at
            FROM heartbeats
            WHERE session_name = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_heartbeat(row)


def recent_heartbeats(
    session_name: str,
    limit: int = 3,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[HeartbeatRecord]:
    """Return up to ``limit`` most-recent heartbeats for ``session_name``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, tmux_window, pane_id, pane_command, pane_dead,
                   log_bytes, snapshot_path, snapshot_hash, created_at
            FROM heartbeats
            WHERE session_name = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (session_name, int(limit)),
        )
        rows = cur.fetchall()
    return [_row_to_heartbeat(row) for row in rows]


def last_heartbeat_at(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> str | None:
    """Return the ISO timestamp of the most-recent heartbeat sweep event.

    Reads from ``messages`` (where heartbeat sweep events have landed
    since #349) rather than the ``heartbeats`` table itself — same
    behaviour as :meth:`StateStore.last_heartbeat_at`.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at FROM messages
            WHERE type = 'event'
              AND scope = 'heartbeat'
              AND subject = 'heartbeat'
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _stamp_str(row[0])


def _row_to_heartbeat(row) -> HeartbeatRecord:
    """Pack a SELECT row into :class:`HeartbeatRecord`.

    Row shape: ``(session_name, tmux_window, pane_id, pane_command,
    pane_dead, log_bytes, snapshot_path, snapshot_hash, created_at)``.
    """
    return HeartbeatRecord(
        session_name=row[0],
        tmux_window=row[1],
        pane_id=row[2],
        pane_command=row[3],
        pane_dead=bool(row[4]),
        log_bytes=int(row[5]),
        snapshot_path=row[6],
        snapshot_hash=row[7],
        created_at=_stamp_str(row[8]),
    )


__all__ = [
    "last_heartbeat_at",
    "latest_heartbeat",
    "recent_heartbeats",
    "record_heartbeat",
]
