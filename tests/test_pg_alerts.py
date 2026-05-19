"""pg-parity tests for the ``pg_alerts`` facade (#1737, Slice K-state-port phase 2d).

Covers cluster B: alerts (which live on the ``messages`` table with
``type='alert'``). Each test applies the canonical pg migrations on a
fresh per-test schema (``pg_schema_pool``), then exercises one slice
of the facade. Mirrors :class:`StateStore` semantics 1:1.

Skipped when neither Docker nor a local pg is available — see
``tests/conftest_pg.py``.
"""

from __future__ import annotations


def _apply_initial_migrations(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


def test_pg_alerts_upsert_and_open(pg_schema_pool):
    """upsert_alert writes; open_alerts surfaces the row."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import open_alerts, upsert_alert

    assert open_alerts(pool=pg_schema_pool) == []

    upsert_alert(
        "session-alpha",
        "pane_dead",
        "error",
        "pane unexpectedly died",
        pool=pg_schema_pool,
    )

    alerts = open_alerts(pool=pg_schema_pool)
    assert len(alerts) == 1
    assert alerts[0].session_name == "session-alpha"
    assert alerts[0].alert_type == "pane_dead"
    assert alerts[0].severity == "error"
    assert alerts[0].message == "pane unexpectedly died"
    assert alerts[0].status == "open"


def test_pg_alerts_upsert_bumps_existing(pg_schema_pool):
    """Re-upserting the same (scope, sender) updates the existing row."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import open_alerts, upsert_alert

    upsert_alert("session-alpha", "pane_dead", "error", "first", pool=pg_schema_pool)
    upsert_alert("session-alpha", "pane_dead", "error", "second", pool=pg_schema_pool)

    alerts = open_alerts(pool=pg_schema_pool)
    # Single row (occurrences bumped, not a new INSERT).
    assert len(alerts) == 1
    assert alerts[0].message == "second"


def test_pg_alerts_clear_alert(pg_schema_pool):
    """clear_alert closes every matching open alert."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import clear_alert, open_alerts, upsert_alert

    upsert_alert("session-alpha", "pane_dead", "error", "msg", pool=pg_schema_pool)
    upsert_alert("session-beta", "pane_dead", "error", "msg2", pool=pg_schema_pool)

    clear_alert("session-alpha", "pane_dead", pool=pg_schema_pool)

    alerts = open_alerts(pool=pg_schema_pool)
    assert len(alerts) == 1
    assert alerts[0].session_name == "session-beta"


def test_pg_alerts_get_and_clear_by_id(pg_schema_pool):
    """get_alert reads any alert by id; clear_alert_by_id closes one row."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import (
        clear_alert_by_id,
        get_alert,
        open_alerts,
        upsert_alert,
    )

    upsert_alert("session-alpha", "pane_dead", "error", "msg", pool=pg_schema_pool)
    alerts = open_alerts(pool=pg_schema_pool)
    alert_id = alerts[0].alert_id
    assert alert_id is not None

    # get_alert reads by id even when open.
    row = get_alert(alert_id, pool=pg_schema_pool)
    assert row is not None
    assert row.status == "open"

    # clear_alert_by_id flips to closed and returns the post-close record.
    closed = clear_alert_by_id(alert_id, pool=pg_schema_pool)
    assert closed is not None
    assert closed.status == "closed"

    # The row is still readable but no longer surfaces in open_alerts.
    after = get_alert(alert_id, pool=pg_schema_pool)
    assert after is not None
    assert after.status == "closed"
    assert open_alerts(pool=pg_schema_pool) == []


def test_pg_alerts_get_alert_missing_returns_none(pg_schema_pool):
    """get_alert on a nonexistent id returns None."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import clear_alert_by_id, get_alert

    assert get_alert(9999, pool=pg_schema_pool) is None
    assert clear_alert_by_id(9999, pool=pg_schema_pool) is None


def test_pg_alerts_open_alerts_sorted_newest_first(pg_schema_pool):
    """open_alerts orders by updated_at DESC."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import open_alerts, upsert_alert

    upsert_alert("s1", "t1", "warn", "first", pool=pg_schema_pool)
    upsert_alert("s2", "t2", "warn", "second", pool=pg_schema_pool)
    upsert_alert("s3", "t3", "warn", "third", pool=pg_schema_pool)

    alerts = open_alerts(pool=pg_schema_pool)
    assert [a.session_name for a in alerts] == ["s3", "s2", "s1"]


def test_pg_alerts_deduplicate(pg_schema_pool):
    """deduplicate_alerts drops dup open rows keyed on (scope, sender), keeps newest."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_alerts import deduplicate_alerts, open_alerts

    # Drop the partial unique index so we can plant explicit duplicates
    # for the cleanup helper to chew on.
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS messages_open_alert_uniq")
        for i, msg in enumerate(["one", "two", "three"]):
            stamp = f"2026-05-19T00:00:0{i}+00:00"
            cur.execute(
                """
                INSERT INTO messages (
                    scope, type, tier, recipient, sender, state,
                    subject, body, payload_json, labels, created_at, updated_at
                )
                VALUES ('s1', 'alert', 'immediate', 'user', 't1', 'open',
                        %s, '', '{}'::jsonb, '[]'::jsonb, %s, %s)
                """,
                (msg, stamp, stamp),
            )
        conn.commit()

    assert len(open_alerts(pool=pg_schema_pool)) == 3
    removed = deduplicate_alerts(pool=pg_schema_pool)
    assert removed == 2
    alerts = open_alerts(pool=pg_schema_pool)
    assert len(alerts) == 1
