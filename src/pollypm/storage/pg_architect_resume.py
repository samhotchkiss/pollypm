"""Postgres facade for the ``architect_resume_tokens`` table (#1737).

This module owns the read/write path for the per-project resume
tokens that :mod:`pollypm.architect_lifecycle` persists when an idle
architect-session is closed. Mirrors the four StateStore methods that
used to back the same table on the sqlite path:

* :func:`upsert_architect_resume_token`
* :func:`get_architect_resume_token`
* :func:`clear_architect_resume_token`
* :func:`list_architect_resume_tokens`

All four functions are module-level (no class) and accept an optional
``pool`` kwarg defaulting to :func:`~pollypm.storage.pg_pool.get_rw_pool`
for mutators or :func:`~pollypm.storage.pg_pool.get_ro_pool` for
readers. Passing a custom pool is the test-harness seam.

Slice K-state-port phase 2b — port of StateStore cluster F.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import ArchitectResumeRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Matches the value StateStore stamps on the sqlite ``captured_at``
    column so dual-write callers (during the cutover) produce
    indistinguishable rows.
    """
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string.

    pg returns ``timestamptz`` columns as ``datetime`` objects;
    StateStore returns them as strings. This helper normalises the
    pg shape to the sqlite shape so the
    :class:`~pollypm.storage.records.ArchitectResumeRecord` dataclass
    sees the same value either way.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def upsert_architect_resume_token(
    *,
    project_key: str,
    provider: str,
    session_id: str,
    last_active_at: str,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Insert-or-replace the resume token row for ``project_key``.

    The sqlite contract uses ISO-8601 strings for ``captured_at`` and
    ``last_active_at``; pg's ``timestamptz`` column accepts the same
    ISO-8601 input directly so callers don't need to convert.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    captured_at = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO architect_resume_tokens (
                project_key, provider, session_id, captured_at, last_active_at
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_key) DO UPDATE SET
                provider = EXCLUDED.provider,
                session_id = EXCLUDED.session_id,
                captured_at = EXCLUDED.captured_at,
                last_active_at = EXCLUDED.last_active_at
            """,
            (project_key, provider, session_id, captured_at, last_active_at),
        )


def get_architect_resume_token(
    project_key: str,
    *,
    pool: "ConnectionPool | None" = None,
) -> ArchitectResumeRecord | None:
    """Return the resume token for ``project_key``, or ``None``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT project_key, provider, session_id, captured_at, last_active_at
            FROM architect_resume_tokens WHERE project_key = %s
            """,
            (project_key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return ArchitectResumeRecord(
        project_key=row[0],
        provider=row[1],
        session_id=row[2],
        captured_at=_stamp_str(row[3]),
        last_active_at=_stamp_str(row[4]),
    )


def clear_architect_resume_token(
    project_key: str,
    *,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Delete the resume token row for ``project_key`` (no-op if missing)."""
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM architect_resume_tokens WHERE project_key = %s",
            (project_key,),
        )


def list_architect_resume_tokens(
    *,
    pool: "ConnectionPool | None" = None,
) -> list[ArchitectResumeRecord]:
    """Return every stored resume token (unordered)."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT project_key, provider, session_id, captured_at, last_active_at
            FROM architect_resume_tokens
            """
        )
        rows = cur.fetchall()
    return [
        ArchitectResumeRecord(
            project_key=row[0],
            provider=row[1],
            session_id=row[2],
            captured_at=_stamp_str(row[3]),
            last_active_at=_stamp_str(row[4]),
        )
        for row in rows
    ]


__all__ = [
    "clear_architect_resume_token",
    "get_architect_resume_token",
    "list_architect_resume_tokens",
    "upsert_architect_resume_token",
]
