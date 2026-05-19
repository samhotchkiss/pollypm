"""Postgres facade for the ``leases`` table (#1737).

This module owns the read/write path for cluster D of the StateStore
port plan: the per-session operator lease ledger. A lease records
which operator (Polly, an architect, a manual override) currently
"owns" a session for the purposes of dispatch and recovery decisions.

Public API:

* :func:`set_lease` — INSERT … ON CONFLICT (session_name) DO UPDATE
* :func:`clear_lease` — DELETE the row for a session
* :func:`get_lease` — SELECT a single row
* :func:`list_leases` — SELECT every row, sorted by session_name

The plan's "acquire/release/extend/expire" naming maps onto the
StateStore signatures actually in use today: ``set_lease`` covers
acquire+extend (idempotent upsert), ``clear_lease`` covers release,
and the supervisor's :meth:`Supervisor.prune_sessions` cascade handles
expiration by deleting rows for vanished sessions through
``pg_sessions.prune_sessions``.

All functions accept an optional ``pool`` kwarg. Default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the mutators and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the readers; passing a
custom pool is the test-harness seam.

Slice K-state-port phase 2d — port of StateStore cluster D.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import LeaseRecord

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


def set_lease(
    session_name: str,
    owner: str,
    note: str = "",
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Insert-or-update a lease row.

    Mirrors :meth:`StateStore.set_lease`: keyed on ``session_name``,
    ``owner`` + ``note`` overwrite on conflict, ``updated_at`` is
    bumped to ``now()``.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO leases (session_name, owner, note, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_name) DO UPDATE SET
                owner = EXCLUDED.owner,
                note = EXCLUDED.note,
                updated_at = EXCLUDED.updated_at
            """,
            (session_name, owner, note, now),
        )


def clear_lease(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Delete the lease row for ``session_name``. Idempotent (no-op when absent)."""
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM leases WHERE session_name = %s",
            (session_name,),
        )


def get_lease(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> LeaseRecord | None:
    """Return the lease row for ``session_name``, or ``None`` if absent."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, owner, note, updated_at
            FROM leases
            WHERE session_name = %s
            """,
            (session_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return LeaseRecord(
        session_name=row[0],
        owner=row[1],
        note=row[2],
        updated_at=_stamp_str(row[3]),
    )


def list_leases(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[LeaseRecord]:
    """Return every lease row, sorted by session_name."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, owner, note, updated_at
            FROM leases
            ORDER BY session_name
            """
        )
        rows = cur.fetchall()
    return [
        LeaseRecord(
            session_name=row[0],
            owner=row[1],
            note=row[2],
            updated_at=_stamp_str(row[3]),
        )
        for row in rows
    ]


__all__ = [
    "clear_lease",
    "get_lease",
    "list_leases",
    "set_lease",
]
