"""Tests for the tiered events-retention sweep on ``messages``.

Issue #342 folded the old ``pollypm.storage.events_retention`` module
into :meth:`SQLAlchemyStore.prune_messages` + the
``events_retention_sweep_handler`` plugin handler. Retention now walks
the unified ``messages`` table (``type='event'``) rather than the
retired legacy ``events`` table.

Two layers covered:

1. Tier classification — every event subject spec'd in the policy falls
   into the right tier and the four tiers are disjoint.
2. ``events_retention_sweep_handler`` — audit / operational /
   high_volume / default rows are deleted per-tier, retention windows
   are honoured, unknown subjects fall through to ``default``.

Runs cheap — each test spins up a real ``SQLAlchemyStore`` under
``tmp_path``. No live ``state.db`` is touched. Targeted invocation:

    HOME=/tmp/pytest-agent-events uv run pytest tests/test_events_retention.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import insert, select

from pollypm.plugins_builtin.core_recurring.plugin import (
    AUDIT_EVENT_SUBJECTS,
    HIGH_VOLUME_EVENT_SUBJECTS,
    OPERATIONAL_EVENT_SUBJECTS,
    events_retention_sweep_handler,
)
from pollypm.store import SQLAlchemyStore
from pollypm.store.schema import messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_event_row(
    store: SQLAlchemyStore,
    *,
    subject: str,
    created_at: datetime,
    payload_json: str = "{}",
) -> int:
    """Insert one ``type='event'`` row with a caller-specified timestamp."""
    row = {
        "scope": "pm",
        "type": "event",
        "tier": "immediate",
        "recipient": "*",
        "sender": "pm",
        "state": "open",
        "subject": subject,
        "body": "",
        "payload_json": payload_json,
        "labels": "[]",
        "created_at": created_at,
        "updated_at": created_at,
    }
    with store.transaction() as conn:
        result = conn.execute(insert(messages), row)
        return int(result.inserted_primary_key[0])


def _count_events(store: SQLAlchemyStore, subject: str) -> int:
    """Return the remaining ``type='event'`` row count for ``subject``."""
    with store.read_engine.connect() as conn:
        result = conn.execute(
            select(messages.c.id).where(
                (messages.c.type == "event")
                & (messages.c.subject == subject)
            )
        )
        return len(result.fetchall())


class _StubSettings:
    """Minimal stand-in for ``config.events`` — only the four window knobs."""

    audit_retention_days = 365
    operational_retention_days = 30
    high_volume_retention_days = 7
    default_retention_days = 30


class _StubProject:
    """Minimal stand-in for ``config.project`` — only ``state_db``."""

    def __init__(self, state_db: Path) -> None:
        self.state_db = state_db


class _StubConfig:
    """Just enough config shape for the retention handler's call sites."""

    def __init__(self, state_db: Path) -> None:
        self.project = _StubProject(state_db)
        self.events = _StubSettings()


class _PatchedStore:
    """Opens a :class:`SQLAlchemyStore` bound to a specific DB path.

    Used to intercept ``_open_msg_store`` + ``_load_config_and_store``
    inside the handler so tests drive a tmp-path DB without standing up
    the full config loader.
    """

    def __init__(self, store: SQLAlchemyStore) -> None:
        self.store = store

    def open_msg(self, _config: Any) -> Any:
        return self.store

    def close_msg(self, _store: Any) -> None:
        # Closing the store here would break the test's post-assert
        # reads — leave the lifecycle to the test fixture.
        return None


@pytest.fixture
def store(tmp_path: Path) -> SQLAlchemyStore:
    """Yields a live store; disposed at teardown."""
    s = SQLAlchemyStore(f"sqlite:///{tmp_path/'state.db'}")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# 1. Tier classification invariants
# ---------------------------------------------------------------------------


class TestTierClassification:
    def test_audit_tier_covers_every_spec_subject(self) -> None:
        expected = {
            "task.approved", "task.rejected", "task.done", "task.claimed",
            "task.queued", "plan.approved", "inbox.message.created", "launch",
            "recovered", "recovery_prompt", "state_drift",
            "persona_swap_detected", "alert", "escalated",
        }
        assert expected.issubset(AUDIT_EVENT_SUBJECTS)

    def test_operational_tier_covers_every_spec_subject(self) -> None:
        expected = {
            "lease", "stop", "send_input", "nudge", "ran",
            "processed", "stabilize_failed", "delivery",
        }
        assert expected.issubset(OPERATIONAL_EVENT_SUBJECTS)

    def test_high_volume_tier_covers_every_spec_subject(self) -> None:
        expected = {"heartbeat", "heartbeat_error", "token_ledger", "scheduled"}
        assert expected.issubset(HIGH_VOLUME_EVENT_SUBJECTS)

    def test_tiers_are_disjoint(self) -> None:
        assert not (AUDIT_EVENT_SUBJECTS & OPERATIONAL_EVENT_SUBJECTS)
        assert not (AUDIT_EVENT_SUBJECTS & HIGH_VOLUME_EVENT_SUBJECTS)
        assert not (OPERATIONAL_EVENT_SUBJECTS & HIGH_VOLUME_EVENT_SUBJECTS)


# ---------------------------------------------------------------------------
# 2. Handler behaviour against a live SQLAlchemyStore
# ---------------------------------------------------------------------------


def _run_handler(store: SQLAlchemyStore, config: _StubConfig) -> dict:
    """Drive the handler against a specific store by patching its openers."""
    patched = _PatchedStore(store)

    from pollypm.plugins_builtin.core_recurring import plugin as _plug

    from contextlib import contextmanager

    @contextmanager
    def _fake_load(_payload):
        yield (config, None)

    with patch.object(_plug, "_load_config_and_store", _fake_load), \
         patch.object(_plug, "_open_msg_store", patched.open_msg), \
         patch.object(_plug, "_close_msg_store", patched.close_msg):
        return events_retention_sweep_handler({})


class TestRetentionSweepHandler:
    def test_audit_subject_older_than_window_is_deleted(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store, subject="task.approved",
            created_at=now - timedelta(days=366),
        )
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        assert result["deleted_audit"] == 1
        assert _count_events(store, "task.approved") == 0

    def test_audit_subject_within_window_is_kept(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store, subject="task.approved",
            created_at=now - timedelta(days=300),
        )
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        assert result["deleted_audit"] == 0
        assert _count_events(store, "task.approved") == 1

    def test_operational_window_respected(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store, subject="lease", created_at=now - timedelta(days=45),
        )
        _insert_event_row(
            store, subject="lease", created_at=now - timedelta(days=10),
        )
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        assert result["deleted_operational"] == 1
        assert _count_events(store, "lease") == 1

    def test_high_volume_window_respected(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store, subject="heartbeat", created_at=now - timedelta(days=10),
        )
        _insert_event_row(
            store, subject="heartbeat", created_at=now - timedelta(days=2),
        )
        _insert_event_row(
            store, subject="token_ledger",
            created_at=now - timedelta(days=8),
        )
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        assert result["deleted_high_volume"] == 2
        assert _count_events(store, "heartbeat") == 1
        assert _count_events(store, "token_ledger") == 0

    def test_unknown_subject_falls_into_default_tier(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store, subject="some.brand.new.type",
            created_at=now - timedelta(days=45),
        )
        _insert_event_row(
            store, subject="some.brand.new.type",
            created_at=now - timedelta(days=10),
        )
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        assert result["deleted_default"] == 1
        assert result["deleted_audit"] == 0
        assert result["deleted_operational"] == 0
        assert result["deleted_high_volume"] == 0
        assert _count_events(store, "some.brand.new.type") == 1

    def test_noop_sweep_emits_no_audit_event(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        """Empty sweep stays silent — no audit row gets appended."""
        before = len(store.query_messages(type="event"))
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        after = len(store.query_messages(type="event"))
        assert result["total"] == 0
        assert before == after  # no ``events.retention_sweep`` row emitted

    def test_active_sweep_emits_audit_event(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store, subject="heartbeat", created_at=now - timedelta(days=10),
        )
        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))
        assert result["deleted_high_volume"] == 1
        audits = store.query_messages(type="event")
        assert any(
            row.get("subject") == "events.retention_sweep" for row in audits
        )

    def test_pinned_event_is_kept_even_when_older_than_retention(
        self, store: SQLAlchemyStore, tmp_path: Path,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            store,
            subject="heartbeat",
            created_at=now - timedelta(days=10),
            payload_json='{"pinned": true, "kind": "first_shipped"}',
        )

        result = _run_handler(store, _StubConfig(tmp_path / "state.db"))

        assert result["deleted_high_volume"] == 0
        rows = store.query_messages(type="event")
        pinned = [
            row for row in rows
            if row.get("subject") == "heartbeat"
            and (row.get("payload") or {}).get("pinned") is True
        ]
        assert len(pinned) == 1
