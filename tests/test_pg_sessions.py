"""pg-parity tests for the ``pg_sessions`` facade (#1737, Slice K-state-port phase 2c).

Covers cluster A: ``sessions`` + ``session_runtime`` + the events trio
(``record_event`` / ``last_event_at`` / ``recent_events``) that lives on
the ``messages`` table.

Each test applies the canonical pg migrations on a fresh per-test
schema (``pg_schema_pool`` from ``conftest_pg``), then exercises one
slice of the facade. The assertions mirror StateStore semantics 1:1
because the migration plan (PRs #1801, #1804, #1807) forbids any
behaviour drift on the cutover.

Skipped when neither Docker nor a local pg with the ``vector``
extension is available — see ``tests/conftest_pg.py``.
"""

from __future__ import annotations


def _apply_initial_migrations(pg_schema_pool) -> None:
    """Apply the canonical schema migrations onto the per-test pool."""
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


# --------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------- #


def test_pg_sessions_upsert_list_roundtrip(pg_schema_pool):
    """upsert_session writes, list_sessions reads, upsert overwrites on conflict."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        get_session_window,
        list_sessions,
        upsert_session,
    )

    assert list_sessions(pool=pg_schema_pool) == []
    assert get_session_window("nope", pool=pg_schema_pool) is None

    upsert_session(
        name="worker-alpha",
        role="worker",
        project="alpha",
        provider="claude-cli",
        account="primary",
        cwd="/tmp/alpha",
        window_name="alpha-1",
        pool=pg_schema_pool,
    )
    rows = list_sessions(pool=pg_schema_pool)
    assert len(rows) == 1
    assert rows[0].name == "worker-alpha"
    assert rows[0].project == "alpha"
    assert rows[0].window_name == "alpha-1"
    assert get_session_window("worker-alpha", pool=pg_schema_pool) == "alpha-1"

    # Re-upserting the same name overwrites (no duplicate row).
    upsert_session(
        name="worker-alpha",
        role="worker",
        project="alpha",
        provider="claude-cli",
        account="secondary",
        cwd="/tmp/alpha2",
        window_name="alpha-2",
        pool=pg_schema_pool,
    )
    rows = list_sessions(pool=pg_schema_pool)
    assert len(rows) == 1
    assert rows[0].account == "secondary"
    assert rows[0].window_name == "alpha-2"


def test_pg_sessions_prune_keeps_listed_drops_others(pg_schema_pool):
    """prune_sessions deletes rows whose name isn't in the valid set."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        list_sessions,
        prune_sessions,
        upsert_session,
    )

    for name, project in [
        ("worker-alpha", "alpha"),
        ("worker-beta", "beta"),
        ("worker-stale", "stale"),
    ]:
        upsert_session(
            name=name,
            role="worker",
            project=project,
            provider="claude-cli",
            account="primary",
            cwd=f"/tmp/{project}",
            window_name=f"{project}-1",
            pool=pg_schema_pool,
        )

    prune_sessions({"worker-alpha", "worker-beta"}, pool=pg_schema_pool)
    rows = sorted(s.name for s in list_sessions(pool=pg_schema_pool))
    assert rows == ["worker-alpha", "worker-beta"]


def test_pg_sessions_prune_empty_set_drops_all(pg_schema_pool):
    """prune_sessions with an empty set deletes every session row."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        list_sessions,
        prune_sessions,
        upsert_session,
    )

    upsert_session(
        name="worker-alpha",
        role="worker",
        project="alpha",
        provider="claude-cli",
        account="primary",
        cwd="/tmp/alpha",
        window_name="alpha-1",
        pool=pg_schema_pool,
    )

    prune_sessions(set(), pool=pg_schema_pool)
    assert list_sessions(pool=pg_schema_pool) == []


def test_pg_sessions_prune_preserves_synthetic_alerts(pg_schema_pool):
    """#1528 carve-out: plan_missing / no_session / no_session_for_assignment:* survive prune."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import prune_sessions

    now_iso = "2026-05-19T00:00:00+00:00"
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        # Plant three open alerts: one synthetic, one regular, one
        # synthetic via the LIKE prefix.
        for scope, sender, subject in [
            ("plan_gate-alpha", "plan_missing", "plan_missing"),
            ("session-alpha", "regular", "alarm"),
            ("task_assignment", "no_session_for_assignment:alpha", "noop"),
        ]:
            cur.execute(
                """
                INSERT INTO messages (
                    scope, type, tier, recipient, sender, state,
                    subject, body, payload_json, labels, created_at, updated_at
                )
                VALUES (%s, 'alert', 'immediate', '*', %s, 'open',
                        %s, '', '{}'::jsonb, '[]'::jsonb, %s, %s)
                """,
                (scope, sender, subject, now_iso, now_iso),
            )
        conn.commit()

    prune_sessions(set(), pool=pg_schema_pool)

    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT sender, state FROM messages WHERE type = 'alert' ORDER BY sender"
        )
        rows = cur.fetchall()
    by_sender = {row[0]: row[1] for row in rows}
    # Synthetic ones stay open; the regular one closes.
    assert by_sender["plan_missing"] == "open"
    assert by_sender["no_session_for_assignment:alpha"] == "open"
    assert by_sender["regular"] == "closed"


# --------------------------------------------------------------------- #
# events (messages table)
# --------------------------------------------------------------------- #


def test_pg_sessions_record_and_recent_events(pg_schema_pool):
    """record_event writes; recent_events returns newest-first with the fallback chain."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        last_event_at,
        recent_events,
        record_event,
    )

    assert recent_events(pool=pg_schema_pool) == []
    assert last_event_at("worker-alpha", "boot", pool=pg_schema_pool) is None

    record_event("worker-alpha", "boot", "starting", pool=pg_schema_pool)
    record_event("worker-alpha", "tick", "tick-1", pool=pg_schema_pool)
    record_event("worker-beta", "boot", "starting beta", pool=pg_schema_pool)

    events = recent_events(limit=10, pool=pg_schema_pool)
    assert len(events) == 3
    # newest-first
    assert events[0].session_name == "worker-beta"
    assert events[0].event_type == "boot"
    assert events[0].message == "starting beta"
    assert events[1].session_name == "worker-alpha"
    assert events[1].event_type == "tick"
    assert events[1].message == "tick-1"

    # last_event_at filters on (scope, subject).
    stamp = last_event_at("worker-alpha", "boot", pool=pg_schema_pool)
    assert isinstance(stamp, str) and stamp
    assert last_event_at("worker-alpha", "nope", pool=pg_schema_pool) is None


def test_pg_sessions_recent_events_limit_honoured(pg_schema_pool):
    """The ``limit`` argument caps the row count."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import recent_events, record_event

    for i in range(5):
        record_event("worker-alpha", "tick", f"tick-{i}", pool=pg_schema_pool)

    events = recent_events(limit=3, pool=pg_schema_pool)
    assert len(events) == 3
    # Returns newest first: tick-4, tick-3, tick-2.
    assert events[0].message == "tick-4"
    assert events[2].message == "tick-2"


# --------------------------------------------------------------------- #
# session_runtime
# --------------------------------------------------------------------- #


def test_pg_session_runtime_upsert_get_list(pg_schema_pool):
    """upsert / get / list roundtrip for session_runtime rows."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        get_session_runtime,
        list_session_runtimes,
        upsert_session_runtime,
    )

    assert get_session_runtime("worker-alpha", pool=pg_schema_pool) is None
    assert list_session_runtimes(pool=pg_schema_pool) == []

    upsert_session_runtime(
        session_name="worker-alpha",
        status="healthy",
        effective_account="primary",
        effective_provider="claude-cli",
        recovery_attempts=0,
        pool=pg_schema_pool,
    )

    row = get_session_runtime("worker-alpha", pool=pg_schema_pool)
    assert row is not None
    assert row.session_name == "worker-alpha"
    assert row.status == "healthy"
    assert row.effective_account == "primary"
    assert row.recovery_attempts == 0
    assert isinstance(row.updated_at, str) and row.updated_at

    upsert_session_runtime(
        session_name="worker-beta",
        status="degraded",
        last_failure_type="rate_limit",
        last_failure_message="quota exhausted",
        pool=pg_schema_pool,
    )

    all_rows = sorted(
        list_session_runtimes(pool=pg_schema_pool),
        key=lambda r: r.session_name,
    )
    assert [r.session_name for r in all_rows] == ["worker-alpha", "worker-beta"]
    assert all_rows[1].status == "degraded"
    assert all_rows[1].last_failure_type == "rate_limit"


def test_pg_session_runtime_partial_upsert_preserves_unset_fields(pg_schema_pool):
    """Re-upserting without a column preserves the existing value (StateStore _UNSET parity)."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        get_session_runtime,
        upsert_session_runtime,
    )

    upsert_session_runtime(
        session_name="worker-alpha",
        status="healthy",
        effective_account="primary",
        effective_provider="claude-cli",
        recovery_attempts=2,
        last_failure_type="rate_limit",
        last_failure_message="initial fail",
        pool=pg_schema_pool,
    )

    # Update only the status; everything else must be preserved.
    upsert_session_runtime(
        session_name="worker-alpha",
        status="degraded",
        pool=pg_schema_pool,
    )

    row = get_session_runtime("worker-alpha", pool=pg_schema_pool)
    assert row is not None
    assert row.status == "degraded"
    assert row.effective_account == "primary"
    assert row.effective_provider == "claude-cli"
    assert row.recovery_attempts == 2
    assert row.last_failure_type == "rate_limit"
    assert row.last_failure_message == "initial fail"


def test_pg_session_runtime_explicit_none_clears_column(pg_schema_pool):
    """Passing ``None`` explicitly writes SQL NULL (distinguished from _UNSET)."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_sessions import (
        get_session_runtime,
        upsert_session_runtime,
    )

    upsert_session_runtime(
        session_name="worker-alpha",
        status="healthy",
        last_failure_type="rate_limit",
        last_failure_message="boom",
        pool=pg_schema_pool,
    )

    upsert_session_runtime(
        session_name="worker-alpha",
        status="healthy",
        last_failure_type=None,
        last_failure_message=None,
        pool=pg_schema_pool,
    )

    row = get_session_runtime("worker-alpha", pool=pg_schema_pool)
    assert row is not None
    assert row.last_failure_type is None
    assert row.last_failure_message is None


# --------------------------------------------------------------------- #
# #1830 — cluster-A caller migration parity
# --------------------------------------------------------------------- #


def test_no_external_supervisor_store_session_callsites() -> None:
    """#1830: cluster-A session/runtime/event ops must not bypass the facade.

    PR #1828 introduced backend-aware dispatch helpers on Supervisor
    but left several external call sites (heartbeats, service_api,
    cockpit, checkpoints, cli) reading & writing session_runtime
    and events directly on ``supervisor.store`` (the legacy
    StateStore). In pg mode that splits cluster-A state between
    Postgres and SQLite — heartbeat recovery status and recent event
    feeds can drift apart.

    This guard asserts that no caller outside ``supervisor.py``
    reaches through ``supervisor.store.*`` for cluster-A surface.
    The supervisor module itself is allowed to keep the SQLite path
    inside its private ``_upsert_session`` / ``_get_session_runtime``
    / ``_recent_events`` / ``_last_event_at`` helpers — those are the
    sqlite branch of the dispatcher.
    """
    import subprocess

    pattern = (
        r"supervisor\.store\.(upsert_session|list_sessions|prune_sessions|"
        r"get_session_window|record_event|last_event_at|recent_events|"
        r"upsert_session_runtime|get_session_runtime|list_session_runtimes)\("
    )
    result = subprocess.run(
        ["git", "grep", "-nE", pattern, "--", "src/"],
        capture_output=True,
        text=True,
        check=False,
    )
    # ``supervisor.py`` is the dispatcher itself and is allowed to
    # call ``self.store.*`` on the sqlite branch — filter those out.
    leaked = [
        line for line in result.stdout.splitlines()
        if line and not line.startswith("src/pollypm/supervisor.py:")
    ]
    assert not leaked, (
        "Unmigrated cluster-A call sites (#1830) — must go through "
        "supervisor.get_session_runtime / .upsert_session_runtime / "
        ".recent_events / .last_event_at:\n" + "\n".join(leaked)
    )
