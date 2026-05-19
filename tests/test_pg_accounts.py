"""pg-parity tests for the ``pg_accounts`` facade (#1737, Slice K-state-port phase 2d).

Covers cluster E: ``account_usage`` + ``account_runtime`` tables.
"""

from __future__ import annotations


def _apply_initial_migrations(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


def test_pg_account_usage_upsert_get(pg_schema_pool):
    """upsert_account_usage writes; get_account_usage reads."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import (
        get_account_usage,
        upsert_account_usage,
    )

    assert get_account_usage("primary", pool=pg_schema_pool) is None

    upsert_account_usage(
        account_name="primary",
        provider="claude-cli",
        plan="max",
        health="ok",
        usage_summary="42%",
        raw_text="raw output",
        used_pct=42,
        remaining_pct=58,
        reset_at="2026-05-20T00:00:00+00:00",
        period_label="weekly",
        pool=pg_schema_pool,
    )

    row = get_account_usage("primary", pool=pg_schema_pool)
    assert row is not None
    assert row.account_name == "primary"
    assert row.provider == "claude-cli"
    assert row.plan == "max"
    assert row.health == "ok"
    assert row.used_pct == 42
    assert row.remaining_pct == 58
    assert row.period_label == "weekly"


def test_pg_account_usage_upsert_overwrites(pg_schema_pool):
    """Re-upserting with the same account_name overwrites."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import (
        get_account_usage,
        upsert_account_usage,
    )

    upsert_account_usage(
        account_name="primary",
        provider="claude-cli",
        plan="max",
        health="ok",
        usage_summary="50%",
        raw_text="",
        pool=pg_schema_pool,
    )
    upsert_account_usage(
        account_name="primary",
        provider="claude-cli",
        plan="max",
        health="degraded",
        usage_summary="90%",
        raw_text="overload",
        pool=pg_schema_pool,
    )

    row = get_account_usage("primary", pool=pg_schema_pool)
    assert row is not None
    assert row.health == "degraded"
    assert row.usage_summary == "90%"


def test_pg_account_usage_list(pg_schema_pool):
    """list_account_usage returns every row, sorted by account_name."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import list_account_usage, upsert_account_usage

    for name in ["charlie", "alpha", "bravo"]:
        upsert_account_usage(
            account_name=name,
            provider="claude-cli",
            plan="max",
            health="ok",
            usage_summary="",
            raw_text="",
            pool=pg_schema_pool,
        )

    rows = list_account_usage(pool=pg_schema_pool)
    assert [r.account_name for r in rows] == ["alpha", "bravo", "charlie"]


def test_pg_account_runtime_upsert_get(pg_schema_pool):
    """upsert_account_runtime / get_account_runtime roundtrip."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import (
        get_account_runtime,
        upsert_account_runtime,
    )

    assert get_account_runtime("primary", pool=pg_schema_pool) is None

    upsert_account_runtime(
        account_name="primary",
        provider="claude-cli",
        status="rate_limited",
        reason="quota exhausted",
        available_at="2026-05-19T18:00:00+00:00",
        refresh_available=True,
        pool=pg_schema_pool,
    )

    row = get_account_runtime("primary", pool=pg_schema_pool)
    assert row is not None
    assert row.status == "rate_limited"
    assert row.reason == "quota exhausted"
    assert row.refresh_available is True


def test_pg_account_runtime_list(pg_schema_pool):
    """list_account_runtimes returns rows sorted by account_name."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import (
        list_account_runtimes,
        upsert_account_runtime,
    )

    for name in ["beta", "alpha"]:
        upsert_account_runtime(
            account_name=name,
            provider="claude-cli",
            status="ok",
            reason="",
            pool=pg_schema_pool,
        )

    rows = list_account_runtimes(pool=pg_schema_pool)
    assert [r.account_name for r in rows] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# #1842 — provider display strings must round-trip through pg_accounts
# ---------------------------------------------------------------------------


def test_pg_account_usage_reset_at_display_string_roundtrip(pg_schema_pool):
    """#1842 — provider ``reset_at`` strings round-trip without corruption.

    The account usage sampler emits human-readable display strings
    such as ``"Monday 1am"``, ``"10:09 on 5 May"``, ``"Apr 10 at 1am"``
    (see ``tests/test_provider_sdk.py`` + the sampler). The pg
    schema's pre-#1842 ``timestamptz`` either rejected these with
    ``InvalidDatetimeFormat`` or silently coerced them to nonsense
    timestamps. After the schema flip to ``text`` they round-trip
    exactly.
    """
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import (
        get_account_usage,
        upsert_account_usage,
    )

    for display in ("Monday 1am", "10:09 on 5 May", "Apr 10 at 1am"):
        upsert_account_usage(
            account_name="primary",
            provider="claude-cli",
            plan="max",
            health="ok",
            usage_summary="42%",
            raw_text="raw",
            reset_at=display,
            period_label="weekly",
            pool=pg_schema_pool,
        )
        row = get_account_usage("primary", pool=pg_schema_pool)
        assert row is not None
        assert row.reset_at == display, (
            f"reset_at must round-trip the provider display string; "
            f"got {row.reset_at!r}, wrote {display!r}"
        )


def test_pg_account_runtime_available_at_display_string_roundtrip(pg_schema_pool):
    """#1842 — ``available_at`` / ``access_expires_at`` round-trip as text."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_accounts import (
        get_account_runtime,
        upsert_account_runtime,
    )

    upsert_account_runtime(
        account_name="primary",
        provider="claude-cli",
        status="rate_limited",
        reason="quota",
        available_at="Monday 1am",
        access_expires_at="Apr 10 at 1am",
        refresh_available=True,
        pool=pg_schema_pool,
    )

    row = get_account_runtime("primary", pool=pg_schema_pool)
    assert row is not None
    assert row.available_at == "Monday 1am"
    assert row.access_expires_at == "Apr 10 at 1am"
