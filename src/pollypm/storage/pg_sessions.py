"""Postgres facade for the ``sessions`` / ``session_runtime`` / events cluster (#1737).

This module owns the read/write path for cluster A of the StateStore
port plan: the per-session tmux/runtime registry (``sessions`` table),
the supervisor's per-session runtime state machine (``session_runtime``
table), and the ``record_event`` / ``last_event_at`` / ``recent_events``
trio that StateStore writes into the unified ``messages`` table with
``type='event'``.

The pg shape mirrors the sqlite shape on
:class:`pollypm.storage.state.StateStore` exactly — same columns, same
``ON CONFLICT`` upsert semantics, same ISO-8601 string stamping on
``timestamptz`` columns so dual-write callers (during the cutover)
produce indistinguishable rows.

Public API:

Sessions:

* :func:`upsert_session` — INSERT … ON CONFLICT (name) DO UPDATE
* :func:`list_sessions` — SELECT … FROM sessions
* :func:`prune_sessions` — delete sessions/leases/session-tier memory
  rows whose ``session_name`` is no longer in the valid set, mirroring
  StateStore's compound prune (see #1528 + #232 for the alert/memory
  carve-outs preserved here)
* :func:`get_session_window` — SELECT window_name FROM sessions WHERE name=…

Events (backed by ``messages`` with ``type='event'``):

* :func:`record_event` — INSERT into ``messages`` with the same
  json-encoded payload sqlite uses
* :func:`last_event_at` — SELECT created_at … ORDER BY id DESC LIMIT 1
* :func:`recent_events` — SELECT N most recent events with the same
  fallback chain sqlite's ``json_extract`` + ``COALESCE`` ladder gave

Session runtime:

* :func:`upsert_session_runtime` — INSERT … ON CONFLICT (session_name)
  DO UPDATE with the same _UNSET sentinel semantics StateStore exposes
* :func:`get_session_runtime` — SELECT … FROM session_runtime WHERE …
* :func:`list_session_runtimes` — SELECT … FROM session_runtime

All functions accept an optional ``pool`` kwarg. The default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the mutators and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the readers; passing a
custom pool is the test-harness seam (the unit tests wire a disposable
pool against a per-test schema).

Slice K-state-port phase 2c — port of StateStore cluster A. The
``sessions``, ``session_runtime``, and ``messages`` tables are migrated
by :mod:`pollypm.storage.pg_schema` so they are always present before
this module runs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import EventRecord, SessionRecord, SessionRuntimeRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


# Sentinel for "argument not provided" on ``upsert_session_runtime`` —
# distinguishes "caller passed ``None``" (intentionally NULL the column)
# from "caller didn't pass anything" (preserve the existing value). The
# same sentinel pattern lives on StateStore as ``_UNSET``; both must
# agree because callers pass either the sqlite path or the pg path and
# the semantics need to match.
_UNSET: object = object()


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Matches the value StateStore stamps on the sqlite ``updated_at`` /
    ``created_at`` columns so dual-write callers (during the cutover)
    produce indistinguishable rows.
    """
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string.

    pg returns ``timestamptz`` columns as ``datetime`` objects;
    StateStore returns them as strings. Normalise to the sqlite shape so
    the record dataclasses see the same value either way.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _opt_stamp_str(value: object) -> str | None:
    """Return ``None`` for NULL, otherwise an ISO-8601 string."""
    if value is None:
        return None
    return _stamp_str(value)


# --------------------------------------------------------------------- #
# sessions table
# --------------------------------------------------------------------- #


def upsert_session(
    *,
    name: str,
    role: str,
    project: str,
    provider: str,
    account: str,
    cwd: str,
    window_name: str,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Insert-or-update a session row.

    Mirrors :meth:`StateStore.upsert_session`: keyed on ``name``, all
    other columns are overwritten on conflict.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (name, role, project, provider, account, cwd, window_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                role = EXCLUDED.role,
                project = EXCLUDED.project,
                provider = EXCLUDED.provider,
                account = EXCLUDED.account,
                cwd = EXCLUDED.cwd,
                window_name = EXCLUDED.window_name
            """,
            (name, role, project, provider, account, cwd, window_name),
        )


def list_sessions(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[SessionRecord]:
    """Return every row in the ``sessions`` table as a SessionRecord."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, role, project, provider, account, cwd, window_name FROM sessions"
        )
        rows = cur.fetchall()
    return [
        SessionRecord(
            name=row[0],
            role=row[1],
            project=row[2],
            provider=row[3],
            account=row[4],
            cwd=row[5],
            window_name=row[6],
        )
        for row in rows
    ]


def prune_sessions(
    valid_session_names: set[str],
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Delete sessions / leases / session-tier memory not in ``valid_session_names``.

    Compound operation that mirrors :meth:`StateStore.prune_sessions`:

    * Delete rows from ``sessions`` whose name isn't in the set.
    * Delete rows from ``leases`` whose session_name isn't in the set.
    * Close every open ``type='alert'`` row in ``messages`` whose scope
      isn't in the set — except the synthetic-scope alerts owned by the
      task_assignment sweep (#1528: ``plan_missing``, ``no_session``,
      ``no_session_for_assignment:*``).
    * Delete ``memory_entries`` with ``scope_tier='session'`` whose
      scope isn't in the set (#232 M03 session-tier auto-purge).

    When ``valid_session_names`` is empty the same operations run with
    no IN-list filter — everything not on the synthetic-scope allowlist
    closes / purges.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        if valid_session_names:
            params = tuple(sorted(valid_session_names))
            cur.execute(
                "DELETE FROM sessions WHERE name <> ALL(%s)",
                (list(params),),
            )
            cur.execute(
                "DELETE FROM leases WHERE session_name <> ALL(%s)",
                (list(params),),
            )
            # #1528 — preserve task_assignment-sweep-owned synthetic
            # alerts. See StateStore.prune_sessions for the full
            # rationale.
            cur.execute(
                """
                UPDATE messages
                SET state = 'closed', closed_at = %s, updated_at = %s
                WHERE type = 'alert'
                  AND state = 'open'
                  AND scope <> ALL(%s)
                  AND sender NOT IN ('plan_missing', 'no_session')
                  AND sender NOT LIKE 'no_session_for_assignment:%%'
                """,
                (now, now, list(params)),
            )
            # #232 M03 — session-tier memory auto-purges when its session ends.
            cur.execute(
                """
                DELETE FROM memory_entries
                WHERE scope_tier = 'session'
                  AND scope <> ALL(%s)
                """,
                (list(params),),
            )
        else:
            cur.execute("DELETE FROM sessions")
            cur.execute("DELETE FROM leases")
            cur.execute(
                """
                UPDATE messages
                SET state = 'closed', closed_at = %s, updated_at = %s
                WHERE type = 'alert' AND state = 'open'
                  AND sender NOT IN ('plan_missing', 'no_session')
                  AND sender NOT LIKE 'no_session_for_assignment:%%'
                """,
                (now, now),
            )
            cur.execute(
                "DELETE FROM memory_entries WHERE scope_tier = 'session'"
            )


def get_session_window(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> str | None:
    """Return the tmux window_name for ``session_name``, or None if missing."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT window_name FROM sessions WHERE name = %s",
            (session_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0])


# --------------------------------------------------------------------- #
# events (backed by messages table with type='event')
# --------------------------------------------------------------------- #


def record_event(
    session_name: str,
    event_type: str,
    message: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Append an event row into ``messages`` with type='event'.

    Mirrors :meth:`StateStore.record_event`: same payload_json shape,
    same fixed columns (scope=session_name, tier='immediate',
    recipient='*', sender=session_name, state='open',
    subject=event_type, body=message, labels='[]').
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    payload = json.dumps(
        {
            "session_name": session_name,
            "event_type": event_type,
            "message": message,
        }
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (
                scope, type, tier, recipient, sender, state,
                subject, body, payload_json, labels, created_at, updated_at
            )
            VALUES (%s, 'event', 'immediate', '*', %s, 'open',
                    %s, %s, %s::jsonb, '[]'::jsonb, %s, %s)
            """,
            (
                session_name,
                session_name,
                event_type,
                message,
                payload,
                now,
                now,
            ),
        )


def last_event_at(
    session_name: str,
    event_type: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> str | None:
    """Return the ISO timestamp of the most recent matching event, or None."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_at FROM messages "
            "WHERE type = 'event' AND scope = %s AND subject = %s "
            "ORDER BY id DESC LIMIT 1",
            (session_name, event_type),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _stamp_str(row[0])


def recent_events(
    limit: int = 20,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[EventRecord]:
    """Return the ``limit`` most recent event rows.

    Mirrors :meth:`StateStore.recent_events`: the ``message`` field
    falls back through body → payload_json.message → payload_json
    string → subject so legacy rows that only set one of these surface
    something sensible.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                scope AS session_name,
                COALESCE(payload_json->>'event_type', subject) AS event_type,
                CASE
                    WHEN body <> '' THEN body
                    WHEN payload_json ? 'message'
                        THEN payload_json->>'message'
                    WHEN payload_json::text <> '{}' THEN payload_json::text
                    ELSE subject
                END AS message,
                created_at
            FROM messages
            WHERE type = 'event'
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        EventRecord(
            session_name=row[0],
            event_type=row[1],
            message=row[2],
            created_at=_stamp_str(row[3]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------- #
# session_runtime table
# --------------------------------------------------------------------- #


def upsert_session_runtime(
    *,
    session_name: str,
    status: str,
    effective_account: str | None | object = _UNSET,
    effective_provider: str | None | object = _UNSET,
    recovery_attempts: int | None | object = _UNSET,
    recovery_window_started_at: str | None | object = _UNSET,
    last_failure_type: str | None | object = _UNSET,
    last_failure_message: str | None | object = _UNSET,
    last_checkpoint_path: str | None | object = _UNSET,
    retry_at: str | None | object = _UNSET,
    last_recovered_at: str | None | object = _UNSET,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Insert-or-update a row in ``session_runtime``.

    Mirrors :meth:`StateStore.upsert_session_runtime`: callers pass only
    the columns they want to change; anything left at the ``_UNSET``
    sentinel preserves the existing value (or its column default when
    no row yet exists). Passing an explicit ``None`` writes SQL NULL.
    """
    current = get_session_runtime(session_name, pool=pool)

    def _resolve(new: object, old_val: object, default: object = None) -> object:
        if new is not _UNSET:
            return new
        return old_val if current else default

    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO session_runtime (
                session_name, status, effective_account, effective_provider,
                recovery_attempts, recovery_window_started_at,
                last_failure_type, last_failure_message, last_checkpoint_path,
                retry_at, last_recovered_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_name) DO UPDATE SET
                status = EXCLUDED.status,
                effective_account = EXCLUDED.effective_account,
                effective_provider = EXCLUDED.effective_provider,
                recovery_attempts = EXCLUDED.recovery_attempts,
                recovery_window_started_at = EXCLUDED.recovery_window_started_at,
                last_failure_type = EXCLUDED.last_failure_type,
                last_failure_message = EXCLUDED.last_failure_message,
                last_checkpoint_path = EXCLUDED.last_checkpoint_path,
                retry_at = EXCLUDED.retry_at,
                last_recovered_at = EXCLUDED.last_recovered_at,
                updated_at = EXCLUDED.updated_at
            """,
            (
                session_name,
                status,
                _resolve(
                    effective_account,
                    current.effective_account if current else None,
                ),
                _resolve(
                    effective_provider,
                    current.effective_provider if current else None,
                ),
                _resolve(
                    recovery_attempts,
                    current.recovery_attempts if current else 0,
                    default=0,
                ),
                _resolve(
                    recovery_window_started_at,
                    current.recovery_window_started_at if current else None,
                ),
                _resolve(
                    last_failure_type,
                    current.last_failure_type if current else None,
                ),
                _resolve(
                    last_failure_message,
                    current.last_failure_message if current else None,
                ),
                _resolve(
                    last_checkpoint_path,
                    current.last_checkpoint_path if current else None,
                ),
                _resolve(
                    retry_at,
                    current.retry_at if current else None,
                ),
                _resolve(
                    last_recovered_at,
                    current.last_recovered_at if current else None,
                ),
                now,
            ),
        )


def get_session_runtime(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> SessionRuntimeRecord | None:
    """Return the runtime row for ``session_name``, or None if absent."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, status, effective_account, effective_provider,
                   recovery_attempts, recovery_window_started_at,
                   last_failure_type, last_failure_message, last_checkpoint_path,
                   retry_at, last_recovered_at, updated_at
            FROM session_runtime
            WHERE session_name = %s
            """,
            (session_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return SessionRuntimeRecord(
        session_name=row[0],
        status=row[1],
        effective_account=row[2],
        effective_provider=row[3],
        recovery_attempts=int(row[4]),
        recovery_window_started_at=_opt_stamp_str(row[5]),
        last_failure_type=row[6],
        last_failure_message=row[7],
        last_checkpoint_path=row[8],
        retry_at=_opt_stamp_str(row[9]),
        last_recovered_at=_opt_stamp_str(row[10]),
        updated_at=_stamp_str(row[11]),
    )


def list_session_runtimes(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[SessionRuntimeRecord]:
    """Return every row in the ``session_runtime`` table."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, status, effective_account, effective_provider,
                   recovery_attempts, recovery_window_started_at,
                   last_failure_type, last_failure_message, last_checkpoint_path,
                   retry_at, last_recovered_at, updated_at
            FROM session_runtime
            """
        )
        rows = cur.fetchall()
    return [
        SessionRuntimeRecord(
            session_name=row[0],
            status=row[1],
            effective_account=row[2],
            effective_provider=row[3],
            recovery_attempts=int(row[4]),
            recovery_window_started_at=_opt_stamp_str(row[5]),
            last_failure_type=row[6],
            last_failure_message=row[7],
            last_checkpoint_path=row[8],
            retry_at=_opt_stamp_str(row[9]),
            last_recovered_at=_opt_stamp_str(row[10]),
            updated_at=_stamp_str(row[11]),
        )
        for row in rows
    ]


__all__ = [
    "get_session_runtime",
    "get_session_window",
    "last_event_at",
    "list_session_runtimes",
    "list_sessions",
    "prune_sessions",
    "recent_events",
    "record_event",
    "upsert_session",
    "upsert_session_runtime",
]
