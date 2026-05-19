"""Postgres facade for the ``token_samples`` + ``token_usage_hourly`` tables (#1737).

This module owns the read/write path for the per-session token
cumulative samples and the hourly aggregate that the cockpit's token
strip / dashboard reads. Mirrors the six StateStore methods that
used to back the same tables on the sqlite path:

* :func:`get_token_sample` — latest sample for a session
* :func:`record_token_sample` — observe + roll forward (delta into hourly bucket)
* :func:`upsert_token_sample` — raw sample upsert (no delta tracking)
* :func:`replace_token_usage_hourly` — atomic delete-and-reinsert
* :func:`recent_token_usage` — hourly slice, newest first
* :func:`daily_token_usage` — per-day rollup over N days

All six functions are module-level (no class) and accept an optional
``pool`` kwarg defaulting to :func:`~pollypm.storage.pg_pool.get_rw_pool`
for mutators or :func:`~pollypm.storage.pg_pool.get_ro_pool` for readers.
Passing a custom pool is the test-harness seam.

Slice K-state-port phase 2b — port of StateStore cluster I.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pollypm.storage.records import TokenSampleRecord, TokenUsageHourlyRecord

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Matches the value StateStore stamps on the sqlite ``observed_at``
    column so dual-write callers (during the cutover) produce
    indistinguishable rows.
    """
    return datetime.now(UTC).isoformat()


def _stamp_str(value: object) -> str:
    """Return ``value`` as an ISO-8601 string.

    pg returns ``timestamptz`` columns as ``datetime`` objects;
    StateStore returns them as strings.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _hour_bucket(observed_at: str) -> str:
    """Truncate an ISO-8601 timestamp to its hour bucket.

    Mirrors the StateStore convention of ``YYYY-MM-DDTHH:00:00+00:00``.
    The hourly aggregate is keyed on this string so a sample observed
    at 14:37 lands in the 14:00 bucket alongside every other 14:xx
    sample from the same account/provider/model/project.
    """
    return observed_at[:13] + ":00:00+00:00"


def get_token_sample(
    session_name: str,
    *,
    pool: "ConnectionPool | None" = None,
) -> TokenSampleRecord | None:
    """Return the latest cumulative-token sample for ``session_name``."""
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT session_name, account_name, provider, model_name,
                   project_key, cumulative_tokens, observed_at
            FROM token_samples
            WHERE session_name = %s
            """,
            (session_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return TokenSampleRecord(
        session_name=row[0],
        account_name=row[1],
        provider=row[2],
        model_name=row[3],
        project_key=row[4],
        cumulative_tokens=int(row[5]),
        observed_at=_stamp_str(row[6]),
    )


def record_token_sample(
    *,
    session_name: str,
    account_name: str,
    provider: str,
    model_name: str,
    project_key: str,
    cumulative_tokens: int,
    observed_at: str | None = None,
    pool: "ConnectionPool | None" = None,
) -> int:
    """Observe a cumulative-token reading and roll forward the hourly bucket.

    Mirrors :meth:`StateStore.record_token_sample`:

    1. Read the previous sample for the session.
    2. If the previous sample is for the same account/provider/model/
       project tuple AND ``cumulative_tokens`` is non-decreasing,
       compute ``delta = cumulative - previous``. Otherwise the
       delta is zero (session swapped accounts / counter reset).
    3. Upsert the new sample.
    4. If ``delta > 0``, increment the corresponding hourly bucket
       row in ``token_usage_hourly``.

    Returns the computed ``delta`` so callers (the supervisor) can
    log per-tick usage. The whole flow runs inside one psycopg
    transaction so a concurrent sample for the same session can't see
    a half-applied state.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    now = observed_at or _now_iso()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_name, provider, model_name, project_key,
                   cumulative_tokens
            FROM token_samples
            WHERE session_name = %s
            FOR UPDATE
            """,
            (session_name,),
        )
        previous = cur.fetchone()
        delta = 0
        if previous is not None:
            if (
                previous[0] == account_name
                and previous[1] == provider
                and previous[2] == model_name
                and previous[3] == project_key
                and int(cumulative_tokens) >= int(previous[4])
            ):
                delta = int(cumulative_tokens) - int(previous[4])
        cur.execute(
            """
            INSERT INTO token_samples (
                session_name, account_name, provider, model_name,
                project_key, cumulative_tokens, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_name) DO UPDATE SET
                account_name = EXCLUDED.account_name,
                provider = EXCLUDED.provider,
                model_name = EXCLUDED.model_name,
                project_key = EXCLUDED.project_key,
                cumulative_tokens = EXCLUDED.cumulative_tokens,
                observed_at = EXCLUDED.observed_at
            """,
            (
                session_name,
                account_name,
                provider,
                model_name,
                project_key,
                int(cumulative_tokens),
                now,
            ),
        )
        if delta > 0:
            bucket = _hour_bucket(now)
            cur.execute(
                """
                INSERT INTO token_usage_hourly (
                    hour_bucket, account_name, provider, model_name,
                    project_key, tokens_used, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (hour_bucket, account_name, provider, model_name, project_key)
                DO UPDATE SET
                    tokens_used = token_usage_hourly.tokens_used + EXCLUDED.tokens_used,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    bucket,
                    account_name,
                    provider,
                    model_name,
                    project_key,
                    int(delta),
                    now,
                ),
            )
    return delta


def upsert_token_sample(
    *,
    session_name: str,
    account_name: str,
    provider: str,
    model_name: str,
    project_key: str,
    cumulative_tokens: int,
    observed_at: str,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Raw upsert of a cumulative-token sample (no delta tracking).

    Used by the transcript-ledger backfill where the hourly aggregate
    is rebuilt wholesale from the transcript JSONL — there's no
    per-tick delta to track, the sample is just bookkeeping for
    ``record_token_sample``'s next call.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO token_samples (
                session_name, account_name, provider, model_name,
                project_key, cumulative_tokens, observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_name) DO UPDATE SET
                account_name = EXCLUDED.account_name,
                provider = EXCLUDED.provider,
                model_name = EXCLUDED.model_name,
                project_key = EXCLUDED.project_key,
                cumulative_tokens = EXCLUDED.cumulative_tokens,
                observed_at = EXCLUDED.observed_at
            """,
            (
                session_name,
                account_name,
                provider,
                model_name,
                project_key,
                int(cumulative_tokens),
                observed_at,
            ),
        )


def replace_token_usage_hourly(
    rows: list[TokenUsageHourlyRecord],
    *,
    account_names: list[str] | None = None,
    pool: "ConnectionPool | None" = None,
) -> None:
    """Atomically delete-and-reinsert the hourly aggregate.

    When ``account_names`` is supplied only those accounts' rows are
    cleared (the transcript backfill processes one account at a time);
    otherwise the whole table is replaced. Runs inside one psycopg
    transaction so a concurrent reader sees either the pre- or
    post-replace state, never a partial wipe.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_rw_pool

        pool = get_rw_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        if account_names:
            cur.execute(
                "DELETE FROM token_usage_hourly WHERE account_name = ANY(%s)",
                (list(account_names),),
            )
        else:
            cur.execute("DELETE FROM token_usage_hourly")
        for row in rows:
            cur.execute(
                """
                INSERT INTO token_usage_hourly (
                    hour_bucket, account_name, provider, model_name,
                    project_key, tokens_used, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row.hour_bucket,
                    row.account_name,
                    row.provider,
                    row.model_name,
                    row.project_key,
                    int(row.tokens_used),
                    row.updated_at,
                ),
            )


def recent_token_usage(
    limit: int = 24,
    *,
    pool: "ConnectionPool | None" = None,
) -> list[TokenUsageHourlyRecord]:
    """Return the most recent hourly aggregate rows (newest hour first).

    Sort order matches StateStore: primary key is ``hour_bucket`` DESC,
    secondary is ``tokens_used`` DESC so a tied bucket prefers the
    heavier consumer.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT hour_bucket, account_name, provider, model_name,
                   project_key, tokens_used, updated_at
            FROM token_usage_hourly
            ORDER BY hour_bucket DESC, tokens_used DESC
            LIMIT %s
            """,
            (int(limit),),
        )
        rows = cur.fetchall()
    return [
        TokenUsageHourlyRecord(
            hour_bucket=row[0],
            account_name=row[1],
            provider=row[2],
            model_name=row[3],
            project_key=row[4],
            tokens_used=int(row[5]),
            updated_at=_stamp_str(row[6]),
        )
        for row in rows
    ]


def daily_token_usage(
    days: int = 30,
    *,
    pool: "ConnectionPool | None" = None,
) -> list[tuple[str, int]]:
    """Return the last ``days`` days as ``(YYYY-MM-DD, total_tokens)`` pairs.

    The list is oldest-first to match the StateStore contract (the
    sqlite branch ``reverse()``\\s the DESC-ordered SELECT). Empty
    buckets are omitted — the caller is expected to fill gaps.
    """
    if pool is None:
        from pollypm.storage.pg_pool import get_ro_pool

        pool = get_ro_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT substr(hour_bucket, 1, 10) AS day,
                   SUM(tokens_used) AS total
            FROM token_usage_hourly
            GROUP BY day
            ORDER BY day DESC
            LIMIT %s
            """,
            (int(days),),
        )
        rows = cur.fetchall()
    return [(row[0], int(row[1])) for row in reversed(rows)]


__all__ = [
    "daily_token_usage",
    "get_token_sample",
    "recent_token_usage",
    "record_token_sample",
    "replace_token_usage_hourly",
    "upsert_token_sample",
]
