"""Postgres facade for the task-assignment notification dedupe (#1737).

This module owns the read/write path for the ``messages`` rows that
back the task-assignment notification dedupe contract (#244 / #952 /
#279). It mirrors the five methods on
:class:`pollypm.storage.state.StateStore` —
``record_notification``, ``claim_notification_slot``,
``update_notification_status``, ``was_notified_within``,
``recent_notifications`` — but talks to the process-wide pg pools
owned by :mod:`pollypm.storage.pg_pool`.

Why a dedicated facade
----------------------

Notifications are stored as rows in the unified ``messages`` table
with ``type = 'task_notification'``. The sqlite path uses
``json_extract(payload_json, '$.field')``; pg uses the ``->>`` /
``->`` operators against the ``jsonb`` column. Keeping the SQL
divergence in one place means callers (task_assignment_notify, the
sweep handler, ``pm task pickup-log``) can ask for a notification
helper without learning the per-backend operator vocabulary.

Public API
----------

All five functions are module-level (no class) and accept an
optional ``pool`` kwarg defaulting to
:func:`~pollypm.storage.pg_pool.get_rw_pool` for mutators or
:func:`~pollypm.storage.pg_pool.get_ro_pool` for readers. Passing a
custom pool is the test-harness seam.

* :func:`record_notification` — append a row (sent / failed) without
  a prior atomic claim. Legacy path retained for callers that pre-date
  :func:`claim_notification_slot`.
* :func:`claim_notification_slot` — atomic check-and-insert under a
  serializable transaction; returns the new ``id`` or ``None`` when
  another claim already exists inside the dedupe window.
* :func:`update_notification_status` — stamp the delivery status (and
  optionally the body) on a previously-claimed row.
* :func:`was_notified_within` — read-side dedupe probe.
* :func:`recent_notifications` — reverse-chronological slice backing
  ``pm task pickup-log``.

Slice K-state-port phase 2 — the pg facade lands here; existing
callers (task_assignment_notify, the sweep handler) keep their
StateStore-shaped interface until the source ripout migrates them
onto a backend-aware ``Store`` accessor.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Return a tz-aware UTC ``now`` used for cutoffs and stamps."""
    return datetime.now(UTC)


def record_notification(
    *,
    session_name: str,
    task_id: str,
    project: str = "",
    message: str = "",
    delivery_status: str = "sent",
    execution_version: int = 0,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Record a task-assignment ping sent to ``session_name``.

    Legacy path used by callers that haven't migrated to
    :func:`claim_notification_slot` (the atomic claim added in #952).
    ``execution_version`` (#279) captures the ``visit`` counter of the
    task's current node execution at ping time so a reject-bounce that
    advances ``visit`` correctly counts as a new ping opportunity.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    payload = json.dumps(
        {
            "project": project,
            "delivery_status": delivery_status,
            "execution_version": int(execution_version),
        }
    )
    now = _now()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (
                scope, type, tier, recipient, sender, state,
                subject, body, payload_json, labels, created_at, updated_at
            )
            VALUES (%s, 'task_notification', 'immediate', %s, %s, 'open',
                    %s, %s, %s::jsonb, '[]'::jsonb, %s, %s)
            """,
            (
                session_name,
                project,
                task_id,
                task_id,
                message,
                payload,
                now,
                now,
            ),
        )


def claim_notification_slot(
    *,
    session_name: str,
    task_id: str,
    window_seconds: int,
    execution_version: int = 0,
    project: str = "",
    message: str = "",
    dedupe_scope: str = "normal",
    pool: "ConnectionPool | None" = None,
) -> int | None:
    """Atomically claim a dedupe slot for a ``(session, task, version)`` ping.

    Mirrors the sqlite implementation's TOCTOU-safe check-and-insert
    (#952). The check + insert run inside one transaction so a
    concurrent caller sees the pending row and returns ``None``.

    * Returns ``None`` when a row already exists for
      ``(session, task, execution_version, dedupe_scope)`` inside the
      ``window_seconds`` window.
    * Otherwise inserts a placeholder row with
      ``delivery_status='pending'`` and returns its ``id``. The caller
      then attempts the send and stamps the resulting status via
      :func:`update_notification_status`.

    Normal-scope claims match any recent scope so a forced kickoff
    also throttles ordinary follow-up sweeps; scoped claims only
    match the same scope so a stale normal row can't suppress the
    forced kickoff.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    scope = (dedupe_scope or "normal").strip() or "normal"
    now = _now()
    cutoff = now - timedelta(seconds=window_seconds)
    payload = json.dumps(
        {
            "project": project,
            "delivery_status": "pending",
            "execution_version": int(execution_version),
            "dedupe_scope": scope,
        }
    )
    with pool.connection() as conn, conn.cursor() as cur:
        # The check + insert run inside the same psycopg transaction
        # (one ``with conn`` block, no explicit COMMIT until exit) so a
        # concurrent claim either sees our row and returns None, or
        # gets blocked on the pg row lock until we commit. The unique
        # constraint is enforced logically by the dedupe-scope match
        # rather than a partial index because the window is dynamic.
        cur.execute(
            """
            SELECT 1 FROM messages
            WHERE type = 'task_notification'
              AND scope = %s
              AND sender = %s
              AND COALESCE((payload_json->>'execution_version')::int, 0) = %s
              AND (
                %s = 'normal'
                OR (
                    %s != 'normal'
                    AND payload_json->>'dedupe_scope' = %s
                )
              )
              AND created_at >= %s
            LIMIT 1
            FOR UPDATE
            """,
            (
                session_name,
                task_id,
                int(execution_version),
                scope,
                scope,
                scope,
                cutoff,
            ),
        )
        if cur.fetchone() is not None:
            return None
        cur.execute(
            """
            INSERT INTO messages (
                scope, type, tier, recipient, sender, state,
                subject, body, payload_json, labels, created_at, updated_at
            )
            VALUES (%s, 'task_notification', 'immediate', %s, %s, 'open',
                    %s, %s, %s::jsonb, '[]'::jsonb, %s, %s)
            RETURNING id
            """,
            (
                session_name,
                project,
                task_id,
                task_id,
                message,
                payload,
                now,
                now,
            ),
        )
        row = cur.fetchone()
    return int(row[0]) if row else None


def update_notification_status(
    notification_id: int,
    *,
    delivery_status: str,
    message: str | None = None,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Update the delivery status / body of a previously-claimed slot.

    Companion to :func:`claim_notification_slot`. ``message=None``
    leaves the body intact; otherwise the canonical message body is
    overwritten with the post-send copy.
    """
    if not notification_id:
        return
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    now = _now()
    with pool.connection() as conn, conn.cursor() as cur:
        if message is None:
            cur.execute(
                """
                UPDATE messages
                SET payload_json = jsonb_set(
                        payload_json,
                        '{delivery_status}',
                        to_jsonb(%s::text)
                    ),
                    updated_at = %s
                WHERE id = %s AND type = 'task_notification'
                """,
                (delivery_status, now, int(notification_id)),
            )
        else:
            cur.execute(
                """
                UPDATE messages
                SET payload_json = jsonb_set(
                        payload_json,
                        '{delivery_status}',
                        to_jsonb(%s::text)
                    ),
                    body = %s,
                    updated_at = %s
                WHERE id = %s AND type = 'task_notification'
                """,
                (delivery_status, message, now, int(notification_id)),
            )


def was_notified_within(
    session_name: str,
    task_id: str,
    window_seconds: int,
    execution_version: int = 0,
    *,
    pool: "ConnectionPool | None" = None,
) -> bool:
    """Return ``True`` if ``(session, task, version)`` was pinged inside the window.

    ``window_seconds`` is the dedupe horizon — 30 min (``1800``) for
    the primary throttle, 5 min (``300``) for the sweeper's
    re-enqueue-avoidance cursor.

    ``execution_version`` (#279) is matched exactly; rows back-fill to
    ``0`` for pre-#279 inserts.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    cutoff = _now() - timedelta(seconds=window_seconds)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM messages
            WHERE type = 'task_notification'
              AND scope = %s
              AND sender = %s
              AND COALESCE((payload_json->>'execution_version')::int, 0) = %s
              AND created_at >= %s
            LIMIT 1
            """,
            (
                session_name,
                task_id,
                int(execution_version),
                cutoff,
            ),
        )
        return cur.fetchone() is not None


def recent_notifications(
    *,
    since_seconds: int | None = None,
    project: str | None = None,
    task_id: str | None = None,
    limit: int = 500,
    pool: "ConnectionPool | None" = None,
) -> list[dict[str, Any]]:
    """Return a reverse-chronological slice of pickup notifications.

    Backs ``pm task pickup-log``. Filters compose with AND.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    clauses: list[str] = []
    params: list[Any] = []
    if since_seconds is not None:
        cutoff = _now() - timedelta(seconds=since_seconds)
        clauses.append("created_at >= %s")
        params.append(cutoff)
    if project is not None:
        clauses.append("recipient = %s")
        params.append(project)
    if task_id is not None:
        clauses.append("sender = %s")
        params.append(task_id)
    where = "WHERE type = 'task_notification'"
    if clauses:
        where += " AND " + " AND ".join(clauses)
    sql = (
        "SELECT scope, sender, recipient, created_at, body, payload_json "
        f"FROM messages {where} ORDER BY created_at DESC LIMIT %s"
    )
    params.append(int(limit))
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        # psycopg returns ``jsonb`` decoded; tolerate string-shaped
        # payloads just in case a future cursor adapter changes the
        # default.
        payload = row[5] if isinstance(row[5], dict) else {}
        if not payload and isinstance(row[5], (str, bytes, bytearray)):
            try:
                decoded = json.loads(row[5])
                if isinstance(decoded, dict):
                    payload = decoded
            except (TypeError, ValueError):
                payload = {}
        created_at = row[3]
        if isinstance(created_at, datetime):
            created_at_str = created_at.isoformat()
        else:
            created_at_str = str(created_at)
        out.append(
            {
                "session_name": row[0],
                "task_id": row[1],
                "project": row[2],
                "notified_at": created_at_str,
                "delivery_status": str(payload.get("delivery_status") or ""),
                "message": row[4],
                "execution_version": int(payload.get("execution_version") or 0),
            }
        )
    return out


__all__ = [
    "claim_notification_slot",
    "record_notification",
    "recent_notifications",
    "update_notification_status",
    "was_notified_within",
]
