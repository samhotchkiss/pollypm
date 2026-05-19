"""Postgres facade for the ``workspace_state`` key/value table (#1737).

This module owns the read/write path for the small key/value table
that backs cascade-level flags (``product_state``, plus future cousins).
It mirrors the sqlite shape on
:class:`pollypm.storage.state.StateStore` — three methods, one row per
key, JSON payload, ``set_at`` + ``set_by`` provenance — but talks to
the process-wide pg pools owned by :mod:`pollypm.storage.pg_pool`.

Public API:

* :func:`set_workspace_state` — INSERT … ON CONFLICT (key) DO UPDATE
* :func:`get_workspace_state` — SELECT value_json FROM workspace_state
* :func:`clear_workspace_state` — DELETE … RETURNING 1

All three accept an optional ``pool`` kwarg. The default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the mutators and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the reader; passing a
custom pool is the test-harness seam (the unit tests wire a
disposable pool against a per-test schema).

Slice K-state-port phase 2 — first cutover of a StateStore method
cluster onto a dedicated pg helper. The pg schema for
``workspace_state`` is migrated by :mod:`pollypm.storage.pg_schema`
(migration 0001) so the table is always present before this module
runs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

    from pollypm.models import PollyPMConfig

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Matches the value StateStore stamps on the sqlite ``set_at``
    column so dual-write callers (during the cutover) produce
    indistinguishable rows.
    """
    return datetime.now(UTC).isoformat()


def set_workspace_state(
    key: str,
    value: dict[str, Any],
    *,
    actor: str = "system",
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> None:
    """Upsert one ``workspace_state`` row.

    ``value`` must be a JSON-serialisable dict (matching the sqlite
    contract); ``actor`` records the writer identity (defaults to
    ``"system"`` for programmatic writes). The pg column is
    ``jsonb`` so callers don't need to opt into ``json_extract``
    semantics for downstream reads.

    Parameters
    ----------
    key:
        The row's primary key.
    value:
        JSON-encodable payload. Stored as ``jsonb``.
    actor:
        Writer identity stamped on ``set_by``.
    pool:
        Optional explicit pool. Defaults to the process-wide RW
        pool from :func:`pollypm.storage.pg_pool.get_rw_pool`.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    payload = json.dumps(value)
    set_at = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspace_state (key, value_json, set_at, set_by)
            VALUES (%s, %s::jsonb, %s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value_json = EXCLUDED.value_json,
                set_at = EXCLUDED.set_at,
                set_by = EXCLUDED.set_by
            """,
            (key, payload, set_at, actor or "system"),
        )


def get_workspace_state(
    key: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> dict[str, Any] | None:
    """Return the JSON payload stored under ``key``, or ``None``.

    Returns ``None`` for missing rows AND for rows whose ``jsonb``
    payload isn't a dict — matching the sqlite contract where a
    non-dict payload is treated as "not set" and the caller falls
    through to its default behaviour.

    Parameters
    ----------
    key:
        The row's primary key.
    pool:
        Optional explicit pool. Defaults to the process-wide RO
        pool from :func:`pollypm.storage.pg_pool.get_ro_pool`.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool(config)
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value_json FROM workspace_state WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — read path must degrade gracefully
        logger.debug("pg_workspace_state: get failed for key=%r: %s", key, exc)
        return None
    if row is None:
        return None
    raw = row[0]
    # psycopg returns ``jsonb`` as already-decoded Python objects by
    # default (dict / list / etc). Tolerate the legacy "stringified
    # JSON" shape just in case a future cursor adapter changes the
    # default — keeps this helper resilient across psycopg versions.
    if isinstance(raw, (bytes, bytearray, str)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    return raw


def clear_workspace_state(
    key: str,
    *,
    pool: "ConnectionPool | None" = None,
    config: "PollyPMConfig | None" = None,
) -> bool:
    """Delete the row at ``key``; return ``True`` iff a row was removed.

    Mirrors the sqlite rowcount-truthy semantics so callers can
    short-circuit on "nothing to clear" without a separate read.

    Parameters
    ----------
    key:
        The row's primary key.
    pool:
        Optional explicit pool. Defaults to the process-wide RW
        pool from :func:`pollypm.storage.pg_pool.get_rw_pool`.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool(config)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM workspace_state WHERE key = %s",
            (key,),
        )
        return (cur.rowcount or 0) > 0


__all__ = [
    "clear_workspace_state",
    "get_workspace_state",
    "set_workspace_state",
]
