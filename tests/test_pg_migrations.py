"""Forward-migration tests for ``pg_migrations`` (issues #1737, #1850).

The applier itself has unit coverage in :mod:`tests/test_pg_schema.py`.
This module exercises the per-version upgrade paths — specifically the
"start from an older schema → apply forward → assert new shape" flow
that :mod:`tests/test_pg_schema.py` does not cover.
"""

from __future__ import annotations


def _column_type(pool, table: str, column: str) -> str | None:
    """Return ``information_schema.columns.data_type`` for ``table.column``."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    return None if row is None else str(row[0])


def test_0003_converts_account_columns_from_timestamptz_to_text(pg_schema_pool):
    """#1850 — old deployments declared the three account columns as
    ``timestamptz``; migration 0003 must convert them to ``text``
    in-place so subsequent upserts of provider display strings
    ("Monday 1am") stop crashing.
    """
    from pollypm.storage.pg_migrations import apply_migrations

    # Create the pre-0003 shape by hand: applier wouldn't naturally
    # reach this state on a fresh schema (0001 already declares the
    # columns as text after PR #1848), so we simulate the legacy
    # deployment that was migrated up to 0002 with the old DDL.
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE account_usage (
                account_name  text PRIMARY KEY,
                provider      text NOT NULL,
                plan          text NOT NULL,
                health        text NOT NULL,
                usage_summary text NOT NULL,
                raw_text      text NOT NULL,
                used_pct      int,
                remaining_pct int,
                reset_at      timestamptz,
                period_label  text,
                updated_at    timestamptz NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE account_runtime (
                account_name        text PRIMARY KEY,
                provider            text NOT NULL,
                status              text NOT NULL,
                reason              text NOT NULL,
                available_at        timestamptz,
                access_expires_at   timestamptz,
                refresh_available   boolean NOT NULL DEFAULT false,
                updated_at          timestamptz NOT NULL
            )
            """
        )
        # Record migrations 0001/0002 as applied so the applier skips
        # straight to 0003 and exercises the forward path under test.
        cur.execute(
            """
            CREATE TABLE schema_migrations (
                version     bigint PRIMARY KEY,
                label       text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "INSERT INTO schema_migrations (version, label) VALUES "
            "(1, '0001_initial'), (2, '0002_alert_dedupe_tuple')"
        )
        conn.commit()

    # Sanity: pre-migration types are ``timestamp with time zone``.
    assert _column_type(pg_schema_pool, "account_usage", "reset_at") == (
        "timestamp with time zone"
    )
    assert _column_type(pg_schema_pool, "account_runtime", "available_at") == (
        "timestamp with time zone"
    )
    assert _column_type(
        pg_schema_pool, "account_runtime", "access_expires_at",
    ) == "timestamp with time zone"

    result = apply_migrations(pg_schema_pool)
    assert [v for v, _ in result.applied] == [3]

    # Post-migration types must be ``text``.
    assert _column_type(pg_schema_pool, "account_usage", "reset_at") == "text"
    assert _column_type(pg_schema_pool, "account_runtime", "available_at") == (
        "text"
    )
    assert _column_type(
        pg_schema_pool, "account_runtime", "access_expires_at",
    ) == "text"


def test_0003_is_idempotent_on_fresh_schema(pg_schema_pool):
    """A fresh install (0001+0002+0003 in one pass) leaves the columns
    declared as ``text``; running the applier a second time must be a
    no-op rather than re-emitting the ALTER COLUMN.
    """
    from pollypm.storage.pg_migrations import apply_migrations

    first = apply_migrations(pg_schema_pool)
    assert [v for v, _ in first.applied] == [1, 2, 3]

    second = apply_migrations(pg_schema_pool)
    assert not second.did_anything

    assert _column_type(pg_schema_pool, "account_usage", "reset_at") == "text"
    assert _column_type(pg_schema_pool, "account_runtime", "available_at") == (
        "text"
    )
    assert _column_type(
        pg_schema_pool, "account_runtime", "access_expires_at",
    ) == "text"


def test_0003_preserves_existing_rows(pg_schema_pool):
    """Pre-existing rows survive the type conversion: their old
    ``timestamptz`` values become their ISO-8601 text representations
    (``USING reset_at::text``).
    """
    from pollypm.storage.pg_migrations import apply_migrations

    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE account_usage (
                account_name  text PRIMARY KEY,
                provider      text NOT NULL,
                plan          text NOT NULL,
                health        text NOT NULL,
                usage_summary text NOT NULL,
                raw_text      text NOT NULL,
                used_pct      int,
                remaining_pct int,
                reset_at      timestamptz,
                period_label  text,
                updated_at    timestamptz NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE account_runtime (
                account_name        text PRIMARY KEY,
                provider            text NOT NULL,
                status              text NOT NULL,
                reason              text NOT NULL,
                available_at        timestamptz,
                access_expires_at   timestamptz,
                refresh_available   boolean NOT NULL DEFAULT false,
                updated_at          timestamptz NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE schema_migrations (
                version     bigint PRIMARY KEY,
                label       text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "INSERT INTO schema_migrations (version, label) VALUES "
            "(1, '0001_initial'), (2, '0002_alert_dedupe_tuple')"
        )
        cur.execute(
            """
            INSERT INTO account_usage
                (account_name, provider, plan, health, usage_summary,
                 raw_text, reset_at, updated_at)
            VALUES
                ('primary', 'claude-cli', 'max', 'ok', '50%', '',
                 TIMESTAMPTZ '2026-05-20T00:00:00+00:00',
                 TIMESTAMPTZ '2026-05-19T00:00:00+00:00')
            """
        )
        conn.commit()

    apply_migrations(pg_schema_pool)

    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reset_at FROM account_usage WHERE account_name = 'primary'"
        )
        row = cur.fetchone()
    assert row is not None
    # Casting a ``timestamptz`` to text yields the canonical pg display
    # string. We don't assert an exact wire shape (it varies with the
    # server TZ + DateStyle), only that the value survived as a
    # non-empty string carrying the year.
    assert isinstance(row[0], str)
    assert "2026" in row[0]
