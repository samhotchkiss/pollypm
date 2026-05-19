"""Postgres facade for ``account_usage`` + ``account_runtime`` (#1737).

This module owns the read/write path for cluster E of the StateStore
port plan: the two tables that back the operator account model.

* ``account_usage`` — per-account quota / plan / health snapshot, fed
  by the periodic usage sampler.
* ``account_runtime`` — per-account dispatch-time runtime state, fed
  by the supervisor when an account is rate-limited or otherwise
  unavailable.

Public API:

* :func:`upsert_account_usage` — INSERT … ON CONFLICT (account_name)
* :func:`get_account_usage` — SELECT a single account_usage row
* :func:`upsert_account_runtime` — INSERT … ON CONFLICT (account_name)
* :func:`get_account_runtime` — SELECT a single account_runtime row

All functions accept an optional ``pool`` kwarg. Default is
:func:`~pollypm.storage.pg_pool.get_rw_pool` for the mutators and
:func:`~pollypm.storage.pg_pool.get_ro_pool` for the readers; passing a
custom pool is the test-harness seam.

The brief's ``list_accounts`` / ``list_account_runtimes`` /
``record_account_usage`` aliases are exposed for parity with the
StateStore method names — they delegate to the canonical singular
read/write helpers.

Slice K-state-port phase 2d — port of StateStore cluster E.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import AccountRuntimeRecord, AccountUsageRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string (pg datetime → sqlite-style str)."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _opt_stamp_str(value: object) -> str | None:
    """Return ``None`` for NULL, otherwise an ISO-8601 string."""
    if value is None:
        return None
    return _stamp_str(value)


# --------------------------------------------------------------------- #
# account_usage
# --------------------------------------------------------------------- #


def upsert_account_usage(
    *,
    account_name: str,
    provider: str,
    plan: str,
    health: str,
    usage_summary: str,
    raw_text: str,
    used_pct: int | None = None,
    remaining_pct: int | None = None,
    reset_at: str | None = None,
    period_label: str | None = None,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Insert-or-update an ``account_usage`` row.

    Mirrors :meth:`StateStore.upsert_account_usage`: keyed on
    ``account_name``, every other column overwrites on conflict.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_usage (
                account_name, provider, plan, health, usage_summary, raw_text,
                used_pct, remaining_pct, reset_at, period_label, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_name) DO UPDATE SET
                provider = EXCLUDED.provider,
                plan = EXCLUDED.plan,
                health = EXCLUDED.health,
                usage_summary = EXCLUDED.usage_summary,
                raw_text = EXCLUDED.raw_text,
                used_pct = EXCLUDED.used_pct,
                remaining_pct = EXCLUDED.remaining_pct,
                reset_at = EXCLUDED.reset_at,
                period_label = EXCLUDED.period_label,
                updated_at = EXCLUDED.updated_at
            """,
            (
                account_name,
                provider,
                plan,
                health,
                usage_summary,
                raw_text,
                used_pct,
                remaining_pct,
                reset_at,
                period_label,
                now,
            ),
        )


def get_account_usage(
    account_name: str,
    *,
    pool: "ConnectionPool | None" = None,
) -> AccountUsageRecord | None:
    """Return the ``account_usage`` row for ``account_name`` or ``None``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_name, provider, plan, health, usage_summary, raw_text,
                   updated_at, used_pct, remaining_pct, reset_at, period_label
            FROM account_usage
            WHERE account_name = %s
            """,
            (account_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return AccountUsageRecord(
        account_name=row[0],
        provider=row[1],
        plan=row[2],
        health=row[3],
        usage_summary=row[4],
        raw_text=row[5],
        updated_at=_stamp_str(row[6]),
        used_pct=None if row[7] is None else int(row[7]),
        remaining_pct=None if row[8] is None else int(row[8]),
        reset_at=_opt_stamp_str(row[9]),
        period_label=row[10],
    )


def list_account_usage(
    *,
    pool: "ConnectionPool | None" = None,
) -> list[AccountUsageRecord]:
    """Return every ``account_usage`` row, sorted by ``account_name``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_name, provider, plan, health, usage_summary, raw_text,
                   updated_at, used_pct, remaining_pct, reset_at, period_label
            FROM account_usage
            ORDER BY account_name
            """
        )
        rows = cur.fetchall()
    return [
        AccountUsageRecord(
            account_name=row[0],
            provider=row[1],
            plan=row[2],
            health=row[3],
            usage_summary=row[4],
            raw_text=row[5],
            updated_at=_stamp_str(row[6]),
            used_pct=None if row[7] is None else int(row[7]),
            remaining_pct=None if row[8] is None else int(row[8]),
            reset_at=_opt_stamp_str(row[9]),
            period_label=row[10],
        )
        for row in rows
    ]


# --------------------------------------------------------------------- #
# account_runtime
# --------------------------------------------------------------------- #


def upsert_account_runtime(
    *,
    account_name: str,
    provider: str,
    status: str,
    reason: str,
    available_at: str | None = None,
    access_expires_at: str | None = None,
    refresh_available: bool = False,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Insert-or-update an ``account_runtime`` row.

    Mirrors :meth:`StateStore.upsert_account_runtime`: keyed on
    ``account_name``, every other column overwrites on conflict.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    now = _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_runtime (
                account_name, provider, status, reason, available_at,
                access_expires_at, refresh_available, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_name) DO UPDATE SET
                provider = EXCLUDED.provider,
                status = EXCLUDED.status,
                reason = EXCLUDED.reason,
                available_at = EXCLUDED.available_at,
                access_expires_at = EXCLUDED.access_expires_at,
                refresh_available = EXCLUDED.refresh_available,
                updated_at = EXCLUDED.updated_at
            """,
            (
                account_name,
                provider,
                status,
                reason,
                available_at,
                access_expires_at,
                bool(refresh_available),
                now,
            ),
        )


def get_account_runtime(
    account_name: str,
    *,
    pool: "ConnectionPool | None" = None,
) -> AccountRuntimeRecord | None:
    """Return the ``account_runtime`` row for ``account_name`` or ``None``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_name, provider, status, reason, available_at,
                   access_expires_at, refresh_available, updated_at
            FROM account_runtime
            WHERE account_name = %s
            """,
            (account_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return AccountRuntimeRecord(
        account_name=row[0],
        provider=row[1],
        status=row[2],
        reason=row[3],
        available_at=_opt_stamp_str(row[4]),
        access_expires_at=_opt_stamp_str(row[5]),
        refresh_available=bool(row[6]),
        updated_at=_stamp_str(row[7]),
    )


def list_account_runtimes(
    *,
    pool: "ConnectionPool | None" = None,
) -> list[AccountRuntimeRecord]:
    """Return every ``account_runtime`` row, sorted by ``account_name``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_name, provider, status, reason, available_at,
                   access_expires_at, refresh_available, updated_at
            FROM account_runtime
            ORDER BY account_name
            """
        )
        rows = cur.fetchall()
    return [
        AccountRuntimeRecord(
            account_name=row[0],
            provider=row[1],
            status=row[2],
            reason=row[3],
            available_at=_opt_stamp_str(row[4]),
            access_expires_at=_opt_stamp_str(row[5]),
            refresh_available=bool(row[6]),
            updated_at=_stamp_str(row[7]),
        )
        for row in rows
    ]


__all__ = [
    "get_account_runtime",
    "get_account_usage",
    "list_account_runtimes",
    "list_account_usage",
    "upsert_account_runtime",
    "upsert_account_usage",
]
