"""pg-parity tests for the ``pg_heartbeats`` facade (#1737, Slice K-state-port phase 2d).

Covers cluster C: ``heartbeats`` table plus the ``last_heartbeat_at``
helper that reads heartbeat sweep events from ``messages``.
"""

from __future__ import annotations


def _apply_initial_migrations(pg_schema_pool) -> None:
    from pollypm.storage.pg_migrations import apply_migrations

    apply_migrations(pg_schema_pool)


def test_pg_heartbeats_record_and_latest(pg_schema_pool):
    """record_heartbeat writes; latest_heartbeat returns the most-recent row."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_heartbeats import latest_heartbeat, record_heartbeat

    assert latest_heartbeat("worker-alpha", pool=pg_schema_pool) is None

    record_heartbeat(
        session_name="worker-alpha",
        tmux_window="alpha-1",
        pane_id="%42",
        pane_command="claude",
        pane_dead=False,
        log_bytes=4096,
        snapshot_path="/tmp/snap-1",
        snapshot_hash="abc123",
        pool=pg_schema_pool,
    )
    record_heartbeat(
        session_name="worker-alpha",
        tmux_window="alpha-1",
        pane_id="%42",
        pane_command="claude",
        pane_dead=True,
        log_bytes=8192,
        snapshot_path="/tmp/snap-2",
        snapshot_hash="def456",
        pool=pg_schema_pool,
    )

    latest = latest_heartbeat("worker-alpha", pool=pg_schema_pool)
    assert latest is not None
    assert latest.session_name == "worker-alpha"
    assert latest.pane_dead is True
    assert latest.log_bytes == 8192
    assert latest.snapshot_path == "/tmp/snap-2"
    assert latest.snapshot_hash == "def456"


def test_pg_heartbeats_recent_limit(pg_schema_pool):
    """recent_heartbeats returns up to ``limit`` rows newest-first."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_heartbeats import recent_heartbeats, record_heartbeat

    for i in range(5):
        record_heartbeat(
            session_name="worker-alpha",
            tmux_window="alpha-1",
            pane_id="%42",
            pane_command="claude",
            pane_dead=False,
            log_bytes=i,
            snapshot_path=f"/tmp/snap-{i}",
            snapshot_hash=f"hash-{i}",
            pool=pg_schema_pool,
        )

    rows = recent_heartbeats("worker-alpha", limit=3, pool=pg_schema_pool)
    assert len(rows) == 3
    assert [r.log_bytes for r in rows] == [4, 3, 2]


def test_pg_heartbeats_per_session_isolation(pg_schema_pool):
    """latest_heartbeat / recent_heartbeats scope by session_name."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_heartbeats import (
        latest_heartbeat,
        recent_heartbeats,
        record_heartbeat,
    )

    record_heartbeat(
        session_name="worker-alpha",
        tmux_window="alpha",
        pane_id="%1",
        pane_command="claude",
        pane_dead=False,
        log_bytes=100,
        snapshot_path="/a",
        snapshot_hash="aa",
        pool=pg_schema_pool,
    )
    record_heartbeat(
        session_name="worker-beta",
        tmux_window="beta",
        pane_id="%2",
        pane_command="codex",
        pane_dead=True,
        log_bytes=200,
        snapshot_path="/b",
        snapshot_hash="bb",
        pool=pg_schema_pool,
    )

    alpha = latest_heartbeat("worker-alpha", pool=pg_schema_pool)
    assert alpha is not None and alpha.log_bytes == 100
    beta = latest_heartbeat("worker-beta", pool=pg_schema_pool)
    assert beta is not None and beta.log_bytes == 200

    assert len(recent_heartbeats("worker-alpha", pool=pg_schema_pool)) == 1
    assert len(recent_heartbeats("missing", pool=pg_schema_pool)) == 0


def test_pg_heartbeats_last_heartbeat_at_from_messages(pg_schema_pool):
    """last_heartbeat_at reads from ``messages`` (heartbeat sweep events)."""
    _apply_initial_migrations(pg_schema_pool)
    from pollypm.storage.pg_heartbeats import last_heartbeat_at

    assert last_heartbeat_at(pool=pg_schema_pool) is None

    now_iso = "2026-05-19T12:00:00+00:00"
    with pg_schema_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (
                scope, type, tier, recipient, sender, state,
                subject, body, payload_json, labels, created_at, updated_at
            )
            VALUES ('heartbeat', 'event', 'immediate', '*', 'sweep', 'open',
                    'heartbeat', 'tick', '{}'::jsonb, '[]'::jsonb, %s, %s)
            """,
            (now_iso, now_iso),
        )
        conn.commit()

    stamp = last_heartbeat_at(pool=pg_schema_pool)
    assert isinstance(stamp, str) and stamp
