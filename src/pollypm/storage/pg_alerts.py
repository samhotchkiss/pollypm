"""Postgres facade for the ``alerts`` cluster on the ``messages`` table (#1737).

This module owns the read/write path for cluster B of the StateStore
port plan. Alerts are stored as ``messages`` rows with ``type='alert'``
— there is no dedicated ``alerts`` table on either backend. The pg
shape mirrors the sqlite shape on :class:`pollypm.storage.state.StateStore`
exactly so dual-write callers (during the cutover) produce
indistinguishable rows.

Public API:

* :func:`upsert_alert` — INSERT … ON CONFLICT (scope, sender) DO UPDATE,
  same race-resilient INTEGRITY-error retry path StateStore implements
  for #1044
* :func:`clear_alert` — close every open alert whose (scope, sender)
  matches
* :func:`open_alerts` — list every open alert, newest-first
* :func:`get_alert` — read a single alert row by id
* :func:`clear_alert_by_id` — close one specific alert row, returning
  the post-close record (or ``None`` when the id was missing)
* :func:`deduplicate_alerts` — keep only the most recently updated
  open row per (scope, sender) tuple

All functions accept an optional ``pool`` kwarg. The default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the mutators and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the readers; passing a
custom pool is the test-harness seam.

Slice K-state-port phase 2d — port of StateStore cluster B.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import AlertRecord

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


def _strip_alert_subject(subject: str) -> str:
    """Strip the ``[Alert] `` prefix StateStore injects on insert."""
    if subject.startswith("[Alert] "):
        return subject[len("[Alert] "):]
    if subject.startswith("[Alert]"):
        return subject[len("[Alert]"):].lstrip()
    return subject


def _safe_payload(raw: object) -> dict:
    """Decode a ``payload_json`` value into a dict, defensively.

    pg returns ``jsonb`` columns as native Python (dict / list / str /
    None) — sqlite returns the raw JSON string. Cover both shapes so
    consumers calling ``.get(...)`` never AttributeError.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _select_open_alert(
    cur,
    session_name: str,
    alert_type: str,
) -> tuple[int | None, int]:
    """Return ``(row_id, occurrences)`` for the open alert, or ``(None, 0)``.

    Mirrors :meth:`StateStore._select_open_alert`. The occurrences
    counter rides in ``payload_json`` to avoid a schema change.
    """
    cur.execute(
        """
        SELECT id, payload_json
        FROM messages
        WHERE type = 'alert'
          AND scope = %s
          AND sender = %s
          AND state = 'open'
        """,
        (session_name, alert_type),
    )
    existing = cur.fetchone()
    if existing is None:
        return None, 0
    payload = _safe_payload(existing[1])
    prior = 0
    raw = payload.get("occurrences", 0)
    if isinstance(raw, int) and raw > 0:
        prior = raw
    return int(existing[0]), prior


def upsert_alert(
    session_name: str,
    alert_type: str,
    severity: str,
    message: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Insert-or-update an open alert keyed on ``(scope=session_name, sender=alert_type)``.

    Mirrors :meth:`StateStore.upsert_alert`. The partial unique index
    ``messages_open_alert_uniq`` is the cross-process race-guard
    (#1044): if two writers both observe "no row" and both INSERT, the
    second one trips a ``psycopg.errors.UniqueViolation`` and we
    rollback + UPDATE the row the other writer just committed.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    subject = f"[Alert] {message}"
    with pool.connection() as conn, conn.cursor() as cur:
        row_id, prior_occurrences = _select_open_alert(
            cur, session_name, alert_type
        )
        payload_json = json.dumps(
            {
                "severity": severity,
                "session_name": session_name,
                "occurrences": prior_occurrences + 1,
            }
        )
        try:
            if row_id is None:
                cur.execute(
                    """
                    INSERT INTO messages (
                        scope, type, tier, recipient, sender, state,
                        subject, body, payload_json, labels, created_at, updated_at
                    )
                    VALUES (%s, 'alert', 'immediate', 'user', %s, 'open',
                            %s, '', %s::jsonb, '[]'::jsonb, %s, %s)
                    """,
                    (
                        session_name,
                        alert_type,
                        subject,
                        payload_json,
                        now,
                        now,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE messages
                    SET subject = %s, payload_json = %s::jsonb, updated_at = %s
                    WHERE id = %s
                    """,
                    (subject, payload_json, now, row_id),
                )
        except Exception as exc:  # noqa: BLE001 — narrow below
            # psycopg.errors.UniqueViolation surfaces as an
            # IntegrityError subclass. Import lazily so the module
            # can load without psycopg installed.
            try:
                from psycopg import errors as _pg_errors
            except ImportError:
                raise
            if not isinstance(exc, _pg_errors.UniqueViolation):
                raise
            # Lost the race against another writer's INSERT. Roll back
            # the partial transaction, re-read the now-visible row, and
            # apply the bump as an UPDATE.
            conn.rollback()
            with conn.cursor() as retry_cur:
                row_id, prior_occurrences = _select_open_alert(
                    retry_cur, session_name, alert_type
                )
                payload_json = json.dumps(
                    {
                        "severity": severity,
                        "session_name": session_name,
                        "occurrences": prior_occurrences + 1,
                    }
                )
                if row_id is None:
                    retry_cur.execute(
                        """
                        INSERT INTO messages (
                            scope, type, tier, recipient, sender, state,
                            subject, body, payload_json, labels, created_at, updated_at
                        )
                        VALUES (%s, 'alert', 'immediate', 'user', %s, 'open',
                                %s, '', %s::jsonb, '[]'::jsonb, %s, %s)
                        """,
                        (
                            session_name,
                            alert_type,
                            subject,
                            payload_json,
                            now,
                            now,
                        ),
                    )
                else:
                    retry_cur.execute(
                        """
                        UPDATE messages
                        SET subject = %s, payload_json = %s::jsonb, updated_at = %s
                        WHERE id = %s
                        """,
                        (subject, payload_json, now, row_id),
                    )


def clear_alert(
    session_name: str,
    alert_type: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Close every open alert whose ``(scope, sender)`` matches.

    Mirrors :meth:`StateStore.clear_alert`: idempotent — calling on a
    cleared (or never-set) alert is a no-op.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE messages
            SET state = 'closed', closed_at = %s, updated_at = %s
            WHERE type = 'alert'
              AND scope = %s
              AND sender = %s
              AND state = 'open'
            """,
            (now, now, session_name, alert_type),
        )


def open_alerts(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> list[AlertRecord]:
    """Return every open alert, newest ``updated_at`` first."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, scope, sender, payload_json, subject, state, created_at, updated_at
            FROM messages
            WHERE type = 'alert' AND state = 'open'
            ORDER BY updated_at DESC
            """
        )
        rows = cur.fetchall()
    return [_row_to_alert(row) for row in rows]


def get_alert(
    alert_id: int,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> AlertRecord | None:
    """Return a single alert row by id, or ``None`` if absent.

    Mirrors :meth:`StateStore.get_alert`: matches by id regardless of
    state, so a closed alert is still readable.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, scope, sender, payload_json, subject, state, created_at, updated_at
            FROM messages
            WHERE id = %s
            """,
            (int(alert_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _row_to_alert(row)


def clear_alert_by_id(
    alert_id: int,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> AlertRecord | None:
    """Close one specific alert row by id.

    Returns the post-close record (or ``None`` when the id was missing).
    Mirrors :meth:`StateStore.clear_alert_by_id`: only flips the state
    on rows that were open — clearing an already-closed row is a no-op
    but still returns the row.
    """
    existing = get_alert(alert_id, pool=pool)
    if existing is None:
        return None
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE messages
            SET state = 'closed', closed_at = %s, updated_at = %s
            WHERE id = %s AND state = 'open'
            """,
            (now, now, int(alert_id)),
        )
    return get_alert(alert_id, pool=pool)


def deduplicate_alerts(
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> int:
    """Drop duplicate open alerts, keeping the most recently updated row.

    Mirrors the body of :meth:`StateStore._deduplicate_alerts`. The
    partial unique index prevents new duplicates; this helper exists
    for the once-per-process cleanup that runs against legacy databases
    bootstrapped before the index was installed. Returns the number of
    rows removed.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM messages
            WHERE type = 'alert' AND state = 'open'
              AND id NOT IN (
                  SELECT MAX(id) FROM messages
                  WHERE type = 'alert' AND state = 'open'
                  GROUP BY scope, sender
              )
            """
        )
        return cur.rowcount or 0


def _row_to_alert(row) -> AlertRecord:
    """Pack a SELECT row into :class:`AlertRecord`.

    Row shape: ``(id, scope, sender, payload_json, subject, state,
    created_at, updated_at)``.
    """
    payload = _safe_payload(row[3])
    return AlertRecord(
        session_name=row[1],
        alert_type=row[2],
        severity=str(payload.get("severity") or ""),
        message=_strip_alert_subject(str(row[4] or "")),
        status=row[5],
        created_at=_stamp_str(row[6]),
        updated_at=_stamp_str(row[7]),
        alert_id=int(row[0]),
    )


__all__ = [
    "clear_alert",
    "clear_alert_by_id",
    "deduplicate_alerts",
    "get_alert",
    "open_alerts",
    "upsert_alert",
]
