"""Tests for the tiered events-retention policy + hourly sweep handler.

Three layers covered:

1. Tier classification — every event_type spec'd in the policy falls into
   the right tier and the four tiers are disjoint.
2. ``sweep_events`` core — respects each tier's retention window; unknown
   types fall through to default; multi-tier batches return correct
   counts; idempotent on a clean table.
3. ``events_retention_sweep_handler`` plugin wiring — loads config,
   drives the core function, and logs an audit event only when
   deletions happened (no-op sweeps stay silent so the handler doesn't
   grow the table it's trying to shrink).

Runs cheap — each test spins up an in-memory-style SQLite file under
``tmp_path``. No live ``state.db`` is touched. Targeted invocation:

    HOME=/tmp/pytest-agent-events uv run pytest tests/test_events_retention.py -q
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.core_recurring.plugin import (
    events_retention_sweep_handler,
)
from pollypm.storage.events_retention import (
    AUDIT_EVENT_TYPES,
    HIGH_VOLUME_EVENT_TYPES,
    OPERATIONAL_EVENT_TYPES,
    RetentionPolicy,
    RetentionSweepResult,
    sweep_events,
)
from pollypm.storage.state import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_event(
    conn: sqlite3.Connection,
    *,
    session_name: str,
    event_type: str,
    message: str,
    created_at: datetime,
) -> int:
    """Insert one row with an arbitrary ``created_at`` — returns row id."""
    cursor = conn.execute(
        "INSERT INTO events (session_name, event_type, message, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_name, event_type, message, created_at.isoformat()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def _row_count(conn: sqlite3.Connection, event_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,),
    ).fetchone()
    return int(row[0]) if row else 0


def _fresh_store(tmp_path: Path) -> StateStore:
    """Build a fresh StateStore so the events table migration runs."""
    return StateStore(tmp_path / "state.db")


# ---------------------------------------------------------------------------
# 1. Tier classification invariants
# ---------------------------------------------------------------------------


class TestTierClassification:
    def test_audit_tier_covers_every_spec_type(self) -> None:
        """Every audit event_type from the spec is classified as audit."""
        expected = {
            "task.approved",
            "task.rejected",
            "task.done",
            "task.claimed",
            "task.queued",
            "plan.approved",
            "inbox.message.created",
            "launch",
            "recovered",
            "recovery_prompt",
            "state_drift",
            "persona_swap_detected",
            "alert",
            "escalated",
        }
        assert expected.issubset(AUDIT_EVENT_TYPES)

    def test_operational_tier_covers_every_spec_type(self) -> None:
        expected = {
            "lease", "stop", "send_input", "nudge", "ran",
            "processed", "stabilize_failed", "delivery",
        }
        assert expected.issubset(OPERATIONAL_EVENT_TYPES)

    def test_high_volume_tier_covers_every_spec_type(self) -> None:
        expected = {"heartbeat", "heartbeat_error", "token_ledger", "scheduled"}
        assert expected.issubset(HIGH_VOLUME_EVENT_TYPES)

    def test_tiers_are_disjoint(self) -> None:
        """No event_type lives in two tiers — the sweep assumes this."""
        assert not (AUDIT_EVENT_TYPES & OPERATIONAL_EVENT_TYPES)
        assert not (AUDIT_EVENT_TYPES & HIGH_VOLUME_EVENT_TYPES)
        assert not (OPERATIONAL_EVENT_TYPES & HIGH_VOLUME_EVENT_TYPES)


# ---------------------------------------------------------------------------
# 2. sweep_events core behaviour
# ---------------------------------------------------------------------------


class TestSweepEventsCore:
    def test_audit_event_older_than_window_is_deleted(
        self, tmp_path: Path,
    ) -> None:
        """An audit row past 365 days is swept."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            old_id = _insert_event(
                store._conn,
                session_name="pm",
                event_type="task.approved",
                message="old approval",
                created_at=now - timedelta(days=366),
            )
            result = sweep_events(store._conn, now=now)
            assert result.deleted_audit == 1
            # Row must be gone.
            row = store._conn.execute(
                "SELECT id FROM events WHERE id = ?", (old_id,),
            ).fetchone()
            assert row is None
        finally:
            store.close()

    def test_audit_event_within_window_is_kept(self, tmp_path: Path) -> None:
        """An audit row within 365 days survives."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            keep_id = _insert_event(
                store._conn,
                session_name="pm",
                event_type="task.approved",
                message="recent approval",
                created_at=now - timedelta(days=300),
            )
            result = sweep_events(store._conn, now=now)
            assert result.deleted_audit == 0
            row = store._conn.execute(
                "SELECT id FROM events WHERE id = ?", (keep_id,),
            ).fetchone()
            assert row is not None
        finally:
            store.close()

    def test_operational_window_respected(self, tmp_path: Path) -> None:
        """Operational events deleted past 30d, kept within 30d."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            _insert_event(
                store._conn, session_name="pm", event_type="lease",
                message="old lease",
                created_at=now - timedelta(days=45),
            )
            _insert_event(
                store._conn, session_name="pm", event_type="lease",
                message="fresh lease",
                created_at=now - timedelta(days=10),
            )
            result = sweep_events(store._conn, now=now)
            assert result.deleted_operational == 1
            assert _row_count(store._conn, "lease") == 1
        finally:
            store.close()

    def test_high_volume_window_respected(self, tmp_path: Path) -> None:
        """High-volume events deleted past 7d, kept within 7d."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            _insert_event(
                store._conn, session_name="pm", event_type="heartbeat",
                message="stale hb",
                created_at=now - timedelta(days=10),
            )
            _insert_event(
                store._conn, session_name="pm", event_type="heartbeat",
                message="fresh hb",
                created_at=now - timedelta(days=2),
            )
            _insert_event(
                store._conn, session_name="pm", event_type="token_ledger",
                message="stale token",
                created_at=now - timedelta(days=8),
            )
            result = sweep_events(store._conn, now=now)
            assert result.deleted_high_volume == 2
            assert _row_count(store._conn, "heartbeat") == 1
            assert _row_count(store._conn, "token_ledger") == 0
        finally:
            store.close()

    def test_unknown_event_type_falls_into_default(
        self, tmp_path: Path,
    ) -> None:
        """An event_type not in any tier follows the default window (30d)."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            _insert_event(
                store._conn,
                session_name="pm",
                event_type="some.brand.new.type",
                message="old",
                created_at=now - timedelta(days=45),
            )
            _insert_event(
                store._conn,
                session_name="pm",
                event_type="some.brand.new.type",
                message="fresh",
                created_at=now - timedelta(days=10),
            )
            result = sweep_events(store._conn, now=now)
            assert result.deleted_default == 1
            assert result.deleted_audit == 0
            assert result.deleted_operational == 0
            assert result.deleted_high_volume == 0
            assert _row_count(store._conn, "some.brand.new.type") == 1
        finally:
            store.close()

    def test_multi_tier_single_pass_counts_each_tier(
        self, tmp_path: Path,
    ) -> None:
        """Several tiers cleaned in one pass — each counter is correct."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            # 2 audit past 365d.
            _insert_event(
                store._conn, session_name="pm", event_type="task.done",
                message="a1", created_at=now - timedelta(days=400),
            )
            _insert_event(
                store._conn, session_name="pm", event_type="launch",
                message="a2", created_at=now - timedelta(days=400),
            )
            # 1 operational past 30d.
            _insert_event(
                store._conn, session_name="pm", event_type="send_input",
                message="o1", created_at=now - timedelta(days=60),
            )
            # 3 high_volume past 7d.
            for i in range(3):
                _insert_event(
                    store._conn, session_name="pm",
                    event_type="heartbeat_error",
                    message=f"hv{i}",
                    created_at=now - timedelta(days=8),
                )
            # 2 default past 30d.
            for i in range(2):
                _insert_event(
                    store._conn, session_name="pm",
                    event_type="weird.type",
                    message=f"d{i}",
                    created_at=now - timedelta(days=40),
                )
            # Fresh rows that must survive across all tiers.
            _insert_event(
                store._conn, session_name="pm", event_type="task.done",
                message="keep", created_at=now - timedelta(days=1),
            )

            result = sweep_events(store._conn, now=now)
            assert result.deleted_audit == 2
            assert result.deleted_operational == 1
            assert result.deleted_high_volume == 3
            assert result.deleted_default == 2
            assert result.total == 8

            # Survivor remains.
            keep_row = store._conn.execute(
                "SELECT COUNT(*) FROM events WHERE message = 'keep'",
            ).fetchone()
            assert int(keep_row[0]) == 1
        finally:
            store.close()

    def test_idempotent_on_clean_state(self, tmp_path: Path) -> None:
        """Second sweep after a clean pass deletes nothing."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            _insert_event(
                store._conn, session_name="pm", event_type="heartbeat",
                message="old", created_at=now - timedelta(days=10),
            )
            first = sweep_events(store._conn, now=now)
            assert first.total == 1
            second = sweep_events(store._conn, now=now)
            assert second.total == 0
            assert second.deleted_audit == 0
            assert second.deleted_operational == 0
            assert second.deleted_high_volume == 0
            assert second.deleted_default == 0
        finally:
            store.close()

    def test_empty_events_table_returns_zero_counts(
        self, tmp_path: Path,
    ) -> None:
        """Sweep on a freshly-migrated (empty) events table is a no-op."""
        store = _fresh_store(tmp_path)
        try:
            result = sweep_events(store._conn)
            assert isinstance(result, RetentionSweepResult)
            assert result.total == 0
        finally:
            store.close()

    def test_custom_policy_overrides_default_windows(
        self, tmp_path: Path,
    ) -> None:
        """A caller-supplied RetentionPolicy is honoured."""
        store = _fresh_store(tmp_path)
        try:
            now = datetime.now(UTC)
            # With the default 365d audit window this row would survive;
            # under a 10-day policy it must be deleted.
            _insert_event(
                store._conn, session_name="pm", event_type="task.approved",
                message="recent-but-over-10d",
                created_at=now - timedelta(days=30),
            )
            policy = RetentionPolicy(
                audit_days=10, operational_days=10,
                high_volume_days=10, default_days=10,
            )
            result = sweep_events(store._conn, policy, now=now)
            assert result.deleted_audit == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# 3. Plugin handler wiring
# ---------------------------------------------------------------------------


class TestRetentionSweepHandler:
    def _write_minimal_config(
        self, tmp_path: Path, state_db: Path,
    ) -> Path:
        """Produce the smallest pollypm.toml the handler can load."""
        cfg_path = tmp_path / "pollypm.toml"
        cfg_path.write_text(
            "[project]\n"
            f'name = "test"\n'
            f'root = "{tmp_path.as_posix()}"\n'
            f'state_db = "{state_db.as_posix()}"\n'
            f'tmux_session = "test"\n'
            "[runtime]\n"
            "[schedulers]\n"
            "[defaults]\n"
        )
        return cfg_path

    def test_handler_sweeps_and_records_audit_event(
        self, tmp_path: Path,
    ) -> None:
        """Handler deletes expired rows and records a summary event."""
        state_db = tmp_path / "state.db"
        # Prime the schema via the StateStore so the handler's own
        # _load_config_and_store path finds the events table.
        prime = StateStore(state_db)
        now = datetime.now(UTC)
        _insert_event(
            prime._conn, session_name="pm", event_type="heartbeat",
            message="old", created_at=now - timedelta(days=10),
        )
        _insert_event(
            prime._conn, session_name="pm", event_type="task.done",
            message="old-audit", created_at=now - timedelta(days=400),
        )
        prime.close()

        cfg_path = self._write_minimal_config(tmp_path, state_db)
        out = events_retention_sweep_handler({"config_path": str(cfg_path)})

        assert out["total"] == 2
        assert out["deleted_high_volume"] == 1
        assert out["deleted_audit"] == 1

        # The handler must have recorded a single retention-sweep event.
        # #349: audit rows now live on the unified ``messages`` table.
        from pollypm.store import SQLAlchemyStore
        msg_store = SQLAlchemyStore(f"sqlite:///{state_db}")
        try:
            rows = msg_store.query_messages(
                type="event",
                scope="system",
                limit=20,
            )
            matches = [
                row for row in rows
                if row.get("subject") == "events.retention_sweep"
            ]
            assert len(matches) == 1
        finally:
            close = getattr(msg_store, "close", None)
            if callable(close):
                close()

    def test_handler_silent_on_no_op_sweep(self, tmp_path: Path) -> None:
        """No deletions → no ``events.retention_sweep`` log row emitted."""
        state_db = tmp_path / "state.db"
        prime = StateStore(state_db)
        # Only fresh rows — nothing to delete.
        now = datetime.now(UTC)
        _insert_event(
            prime._conn, session_name="pm", event_type="heartbeat",
            message="fresh", created_at=now - timedelta(days=1),
        )
        prime.close()

        cfg_path = self._write_minimal_config(tmp_path, state_db)
        out = events_retention_sweep_handler({"config_path": str(cfg_path)})
        assert out["total"] == 0

        verify = StateStore(state_db)
        try:
            row = verify._conn.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type = 'events.retention_sweep'",
            ).fetchone()
            # Critical: no self-logging when nothing happened — otherwise
            # the handler would grow the table it's pruning.
            assert int(row[0]) == 0
        finally:
            verify.close()
