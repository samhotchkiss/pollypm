"""Pg re-coverage for the events-retention tiered sweep (#1789).

Slice K-tests part 5 (#1737, commit 04285152) deleted
``tests/test_events_retention.py`` because it used SQLAlchemy
primitives (``sqlalchemy.insert(messages)`` to seed rows with explicit
``created_at`` stamps and ``store.read_engine`` for counting). The
retention sweep handler itself was ported to the typed
:meth:`Store.prune_messages` in #1820 so it works against both
backends. This module re-locks the sweep's contracts against
:class:`pollypm.store.backends.pg_store.PgStore`.

Coverage
--------

* Tier-classification invariants — every spec subject lands in the
  right tier; the four tiers are disjoint.
* Retention-sweep handler — audit / operational / high_volume /
  default windows respected; unknown subjects fall into default tier;
  no-op sweeps stay silent; active sweeps emit a single
  ``events.retention_sweep`` audit row.

Backend
-------

The handler reads its store via :func:`pollypm.store.registry.get_store`;
we monkeypatch ``_open_msg_store`` (and ``_load_config_and_store``) so
the per-test pg schema is what the sweep walks. Seeding rows with
synthetic ``created_at`` stamps uses raw SQL through the schema pool —
``PgStore`` doesn't expose a public hook for ``created_at`` override,
so a direct UPDATE on the per-test schema is the only way to age rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pollypm.plugins_builtin.core_recurring.maintenance import (
    AUDIT_EVENT_SUBJECTS,
    HIGH_VOLUME_EVENT_SUBJECTS,
    OPERATIONAL_EVENT_SUBJECTS,
)
from pollypm.plugins_builtin.core_recurring.plugin import (
    events_retention_sweep_handler,
)


# ---------------------------------------------------------------------------
# Stub config shapes the retention handler expects.
# ---------------------------------------------------------------------------


class _StubSettings:
    """Match ``config.events`` — only the four retention windows."""

    audit_retention_days = 365
    operational_retention_days = 30
    high_volume_retention_days = 7
    default_retention_days = 30


class _StubProject:
    def __init__(self, state_db: Path) -> None:
        self.state_db = state_db


class _StubStorage:
    backend = "postgres"


class _StubConfig:
    """Minimum config shape consumed by the retention handler."""

    def __init__(self, state_db: Path) -> None:
        self.project = _StubProject(state_db)
        self.events = _StubSettings()
        self.storage = _StubStorage()


# ---------------------------------------------------------------------------
# Helpers — seed events with synthetic timestamps.
# ---------------------------------------------------------------------------


def _insert_event_row(
    store: Any,
    pool: Any,
    *,
    subject: str,
    created_at: datetime,
    payload_json: str = "{}",
) -> int:
    """Insert one ``type='event'`` row with a caller-specified timestamp.

    :meth:`PgStore.record_event` doesn't expose ``created_at``, so we
    insert via raw SQL on the per-test schema pool. The shape mirrors
    :meth:`PgStore.record_event` (subject + payload_json + kind), with
    an explicit ``created_at`` for the retention cutoff to chew on.
    """
    sql = (
        "INSERT INTO messages ("
        "scope, type, tier, recipient, sender, state, subject, body, "
        "payload_json, labels, kind, created_at, updated_at"
        ") VALUES ("
        "%s, 'event', 'immediate', '*', %s, 'open', %s, '', "
        "%s::jsonb, '[]'::jsonb, 'activity_event', %s, %s"
        ") RETURNING id"
    )
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql,
            ("pm", "pm", subject, payload_json, created_at, created_at),
        )
        row = cur.fetchone()
        conn.commit()
    return int(row[0]) if row else 0


def _count_events(store: Any, subject: str) -> int:
    """Remaining ``type='event'`` row count for ``subject``."""
    rows = store.query_messages(type="event", subject=subject)
    return len(rows)


def _run_handler(
    store: Any, config: _StubConfig, monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Drive the retention sweep against a specific store + config."""
    from pollypm.plugins_builtin.core_recurring import plugin as _plug

    @contextmanager
    def _fake_load(_payload):
        yield (config, None)

    with patch.object(_plug, "_load_config_and_store", _fake_load), \
            patch.object(_plug, "_open_msg_store", lambda _config: store), \
            patch.object(_plug, "_close_msg_store", lambda _store: None):
        return events_retention_sweep_handler({})


@pytest.fixture()
def pg_msg_store(pg_schema_pool):
    """Per-test :class:`PgStore` bound to the schema pool."""
    from pollypm.store.backends.pg_store import PgStore

    return PgStore(url="postgresql://test/ignored")


# ---------------------------------------------------------------------------
# 1. Tier classification invariants — ported verbatim from the
#    deleted suite. These are backend-neutral but live here so the
#    retention coverage is in one module.
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
# 2. Handler behaviour against PgStore — ported scenarios.
# ---------------------------------------------------------------------------


class TestRetentionSweepHandlerPg:
    """Each tier's retention window must be enforced; unknown subjects
    fall through to the default tier; pinned events survive every tier.
    """

    def test_audit_subject_older_than_window_is_deleted(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="task.approved",
            created_at=now - timedelta(days=366),
        )
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        assert result["deleted_audit"] == 1
        assert _count_events(pg_msg_store, "task.approved") == 0

    def test_audit_subject_within_window_is_kept(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="task.approved",
            created_at=now - timedelta(days=300),
        )
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        assert result["deleted_audit"] == 0
        assert _count_events(pg_msg_store, "task.approved") == 1

    def test_operational_window_respected(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="lease", created_at=now - timedelta(days=45),
        )
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="lease", created_at=now - timedelta(days=10),
        )
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        assert result["deleted_operational"] == 1
        assert _count_events(pg_msg_store, "lease") == 1

    def test_high_volume_window_respected(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="heartbeat", created_at=now - timedelta(days=10),
        )
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="heartbeat", created_at=now - timedelta(days=2),
        )
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="token_ledger",
            created_at=now - timedelta(days=8),
        )
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        assert result["deleted_high_volume"] == 2
        assert _count_events(pg_msg_store, "heartbeat") == 1
        assert _count_events(pg_msg_store, "token_ledger") == 0

    def test_unknown_subject_falls_into_default_tier(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="some.brand.new.type",
            created_at=now - timedelta(days=45),
        )
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="some.brand.new.type",
            created_at=now - timedelta(days=10),
        )
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        assert result["deleted_default"] == 1
        assert result["deleted_audit"] == 0
        assert result["deleted_operational"] == 0
        assert result["deleted_high_volume"] == 0
        assert _count_events(pg_msg_store, "some.brand.new.type") == 1

    def test_noop_sweep_emits_no_audit_event(
        self, pg_msg_store, tmp_path, monkeypatch,
    ) -> None:
        """Empty sweep stays silent — no ``events.retention_sweep`` row."""
        before = len(pg_msg_store.query_messages(type="event"))
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        after = len(pg_msg_store.query_messages(type="event"))
        assert result["total"] == 0
        assert before == after

    def test_active_sweep_emits_audit_event(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        """A sweep that deletes rows emits one audit row recording the totals."""
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="heartbeat", created_at=now - timedelta(days=10),
        )
        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )
        assert result["deleted_high_volume"] == 1
        audits = pg_msg_store.query_messages(type="event")
        assert any(
            row.get("subject") == "events.retention_sweep" for row in audits
        )

    def test_pinned_event_is_kept_even_when_older_than_retention(
        self, pg_msg_store, pg_schema_pool, tmp_path, monkeypatch,
    ) -> None:
        """``payload.pinned == True`` survives every tier's prune."""
        now = datetime.now(timezone.utc)
        _insert_event_row(
            pg_msg_store, pg_schema_pool,
            subject="heartbeat",
            created_at=now - timedelta(days=10),
            payload_json='{"pinned": true, "kind": "first_shipped"}',
        )

        result = _run_handler(
            pg_msg_store, _StubConfig(tmp_path / "state.db"), monkeypatch,
        )

        assert result["deleted_high_volume"] == 0
        rows = pg_msg_store.query_messages(type="event")
        pinned = [
            row for row in rows
            if row.get("subject") == "heartbeat"
            and (row.get("payload") or {}).get("pinned") is True
        ]
        assert len(pinned) == 1
