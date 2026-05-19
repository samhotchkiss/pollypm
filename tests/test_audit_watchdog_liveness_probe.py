"""Tests for the audit_watchdog liveness auto-heal probe (#1815).

The probe is registered as a 1-minute recurring handler. It:

1. Scans the central audit tail for the freshest ``heartbeat.tick``.
2. If the freshest tick is older than 3x the audit_watchdog schedule
   (15 min default), force-clears any orphaned ``claimed`` rows
   on the pg job queue and re-enqueues the ``audit.watchdog`` job
   with its canonical dedupe key.
3. Emits a ``watchdog_silent`` alert so the auto-heal is visible
   in ``pm alerts``.

These tests pin the wedge symptom from #1815 and prove the probe
breaks it without manual intervention. They MUST run before the
``pm rail-daemon`` ever boots in production — if the probe is dead,
the V1 RC audit_watchdog liveness contract is dead with it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pollypm.audit.watchdog import (
    EVENT_HEARTBEAT_TICK,
    freshest_heartbeat_tick_ts,
)
from pollypm.plugins_builtin.core_recurring.audit_watchdog import (
    AUDIT_WATCHDOG_HANDLER_NAME,
)
from pollypm.plugins_builtin.core_recurring.maintenance import (
    HEAL_THROTTLE_SECONDS,
    LIVENESS_STALE_THRESHOLD_SECONDS,
    audit_watchdog_liveness_probe_handler,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_audit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the central-tail root so tests never touch ~/.pollypm/."""
    audit_home = tmp_path / "audit-home"
    audit_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("POLLYPM_AUDIT_HOME", str(audit_home))
    return audit_home


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _write_tick(
    audit_home: Path, *, ts: datetime, project: str = "demo",
    actor: str = "audit_watchdog",
) -> None:
    """Append a single ``heartbeat.tick`` line to ``<audit_home>/<project>.jsonl``."""
    record = {
        "schema": 1,
        "ts": ts.isoformat(),
        "project": project,
        "event": EVENT_HEARTBEAT_TICK,
        "subject": "audit_watchdog",
        "actor": actor,
        "status": "ok",
        "metadata": {"cadence": "@every 5m"},
    }
    path = audit_home / f"{project}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record))
        fh.write("\n")


# ---------------------------------------------------------------------------
# Fake job queue — captures every method the probe touches.
# ---------------------------------------------------------------------------


class _FakeQueue:
    """In-memory queue stub that mirrors the parts of ``JobQueue`` the probe uses."""

    def __init__(self, *, dedupe_active: bool = False) -> None:
        self._dedupe_active = dedupe_active
        self.recover_calls: int = 0
        self.enqueued: list[tuple[str, str | None]] = []
        self.dedupe_checks: list[tuple[str, datetime]] = []

    def has_recent_or_active_dedupe(
        self,
        dedupe_key: str,
        *,
        since: datetime,
    ) -> bool:
        self.dedupe_checks.append((dedupe_key, since))
        return self._dedupe_active

    def recover_orphaned_claims(self) -> tuple[int, int]:
        self.recover_calls += 1
        # Simulate one orphaned claim being recovered — that's the
        # observable signal the wedge described in #1815 leaves behind
        # when ``audit.watchdog`` itself was the stuck handler.
        return (1, 0)

    def enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        max_attempts: int | None = None,
    ) -> int:
        self.enqueued.append((handler_name, dedupe_key))
        return 7


# ---------------------------------------------------------------------------
# freshest_heartbeat_tick_ts
# ---------------------------------------------------------------------------


def test_freshest_heartbeat_tick_returns_none_when_no_tail(
    _isolate_audit_home: Path,
) -> None:
    """No central tail files → no fresh tick."""
    assert freshest_heartbeat_tick_ts() is None


def test_freshest_heartbeat_tick_returns_none_when_no_tick_events(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """Central tail exists but has no ``heartbeat.tick`` rows → None.

    This is the #1815 Bug A symptom that was the gating clue:
    audit files exist (with churn from work_db.opened etc.) but
    ``heartbeat.tick`` has never landed.
    """
    other_record = {
        "schema": 1,
        "ts": now.isoformat(),
        "project": "demo",
        "event": "work_db.opened",
        "subject": "demo",
        "actor": "system",
        "status": "ok",
        "metadata": {},
    }
    path = _isolate_audit_home / "demo.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(other_record))
        fh.write("\n")
    assert freshest_heartbeat_tick_ts() is None


def test_freshest_heartbeat_tick_picks_max_across_files(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """Returns the max ts across all *.jsonl files in the audit home."""
    _write_tick(_isolate_audit_home, ts=now - timedelta(minutes=10), project="alpha")
    _write_tick(_isolate_audit_home, ts=now - timedelta(minutes=3), project="bravo")
    _write_tick(_isolate_audit_home, ts=now - timedelta(minutes=20), project="charlie")
    fresh = freshest_heartbeat_tick_ts()
    assert fresh is not None
    assert abs((fresh - (now - timedelta(minutes=3))).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# Liveness probe — pinning #1815 behaviour
# ---------------------------------------------------------------------------


def test_probe_noop_when_tail_is_fresh(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """A tick 2 minutes old leaves the queue untouched.

    Pins the negative — we DON'T want the probe re-enqueueing the
    cadence job on every minute, or it would defeat the dedupe.
    """
    _write_tick(_isolate_audit_home, ts=now - timedelta(minutes=2))
    queue = _FakeQueue()
    summary = audit_watchdog_liveness_probe_handler(
        {}, queue=queue, now=now,
    )
    assert summary["stale"] is False
    assert summary["action"] == "none"
    assert queue.recover_calls == 0
    assert queue.enqueued == []


def test_probe_heals_when_tail_is_stale(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """Tail tick >15 minutes old triggers recover + re-enqueue.

    This is the #1815 wedge symptom: the freshest ``heartbeat.tick``
    in ``~/.pollypm/audit/*.jsonl`` is hours old. The probe must
    recover orphaned claims (which may be holding the dedupe slot
    open) and re-enqueue the cadence job.
    """
    _write_tick(_isolate_audit_home, ts=now - timedelta(hours=9))
    queue = _FakeQueue(dedupe_active=False)
    summary = audit_watchdog_liveness_probe_handler(
        {}, queue=queue, now=now,
    )
    assert summary["stale"] is True
    assert summary["action"] == "healed"
    assert summary["age_seconds"] is not None
    assert summary["age_seconds"] >= LIVENESS_STALE_THRESHOLD_SECONDS
    assert queue.recover_calls == 1
    assert queue.enqueued == [
        (AUDIT_WATCHDOG_HANDLER_NAME, AUDIT_WATCHDOG_HANDLER_NAME),
    ]


def test_probe_heals_when_tail_has_no_ticks_at_all(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """Zero ``heartbeat.tick`` events anywhere → treated as the worst-case wedge.

    This is the exact #1815 Bug A symptom: the watchdog has
    NEVER successfully emitted a tick. The probe should not
    require an existing tick to fire; the absence IS the alarm.
    """
    queue = _FakeQueue(dedupe_active=False)
    summary = audit_watchdog_liveness_probe_handler(
        {}, queue=queue, now=now,
    )
    assert summary["stale"] is True
    assert summary["action"] == "healed"
    assert summary["age_seconds"] is None
    assert queue.recover_calls == 1
    assert len(queue.enqueued) == 1


def test_probe_throttles_when_recent_heal_already_landed(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """A recent ``audit.watchdog`` re-enqueue suppresses a second heal.

    The probe checks ``has_recent_or_active_dedupe`` before
    re-enqueueing. A True return there means a previous probe
    (within HEAL_THROTTLE_SECONDS) already healed; the alert
    that probe raised remains open, so this run is a no-op.
    """
    _write_tick(_isolate_audit_home, ts=now - timedelta(hours=9))
    queue = _FakeQueue(dedupe_active=True)
    summary = audit_watchdog_liveness_probe_handler(
        {}, queue=queue, now=now,
    )
    assert summary["stale"] is True
    assert summary["action"] == "throttled"
    assert queue.recover_calls == 0
    assert queue.enqueued == []
    # The throttle window is HEAL_THROTTLE_SECONDS back from now.
    assert len(queue.dedupe_checks) == 1
    key, since = queue.dedupe_checks[0]
    assert key == AUDIT_WATCHDOG_HANDLER_NAME
    assert abs((now - since).total_seconds() - HEAL_THROTTLE_SECONDS) < 1.0


def test_probe_respects_custom_stale_threshold(
    _isolate_audit_home: Path,
    now: datetime,
) -> None:
    """Test seam — tighter threshold lets the probe fire earlier."""
    _write_tick(_isolate_audit_home, ts=now - timedelta(seconds=120))
    queue = _FakeQueue()
    summary = audit_watchdog_liveness_probe_handler(
        {}, queue=queue, now=now, stale_threshold_seconds=60.0,
    )
    assert summary["stale"] is True
    assert summary["action"] == "healed"
    assert queue.enqueued == [
        (AUDIT_WATCHDOG_HANDLER_NAME, AUDIT_WATCHDOG_HANDLER_NAME),
    ]
