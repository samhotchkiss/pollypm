"""Tests for the ``work.progress_sweep`` roster handler — #249.

Complements the 10s ``session.health_sweep`` by looking at work-service
state (stale in_progress claims) every 5 minutes and resume-pinging the
claimant session via the existing ``task_assignment_notify`` path.

Key contracts verified here:

* Fires a resume ping exactly once for a stuck in_progress task.
* Dedupes on the 30-min task_notifications window (never double-fires).
* Skips sessions with an active turn indicator (don't ping mid-work).
* Skips sessions with a recent event in the ledger (< 30min).
* Handler is registered on the core_recurring roster at @every 5m.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.plugins_builtin.core_recurring.plugin import (
    plugin as core_plugin,
    work_progress_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    _RuntimeServices,
)
from pollypm.storage.state import StateStore
from pollypm.work import task_assignment as bus
from pollypm.work.sqlite_service import SQLiteWorkService


# ---------------------------------------------------------------------------
# Fakes — mirror the pattern used in test_task_assignment_notify
# ---------------------------------------------------------------------------


@dataclass
class FakeHandle:
    name: str


@dataclass
class FakeSessionService:
    handles: list[FakeHandle]
    sent: list[tuple[str, str]] = field(default_factory=list)
    busy: set[str] = field(default_factory=set)

    def list(self) -> list[FakeHandle]:
        return list(self.handles)

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        self.sent.append((name, text))

    def is_turn_active(self, name: str) -> bool:
        return name in self.busy


def _claim_task(work: SQLiteWorkService, project: str) -> str:
    """Create + queue + claim a task; returns its id."""
    task = work.create(
        title="Do the thing",
        description="desc",
        type="task",
        project=project,
        flow_template="standard",
        roles={"worker": "agent-1", "reviewer": "agent-2"},
        priority="normal",
    )
    work.queue(task.task_id, "pm")
    work.claim(task.task_id, "agent-1")
    return task.task_id


# ---------------------------------------------------------------------------
# Roster registration — exact cadence pinning
# ---------------------------------------------------------------------------


class TestWorkProgressSweepRegistration:
    def test_handler_is_registered(self) -> None:
        from pollypm.jobs import JobHandlerRegistry
        from pollypm.plugin_api.v1 import JobHandlerAPI

        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="core_recurring")
        core_plugin.register_handlers(api)
        assert "work.progress_sweep" in registry.names()

    def test_roster_cadence_is_5_minutes(self) -> None:
        from pollypm.heartbeat import Roster
        from pollypm.heartbeat.roster import EverySchedule
        from pollypm.plugin_api.v1 import RosterAPI

        roster = Roster()
        api = RosterAPI(roster, plugin_name="core_recurring")
        core_plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        entry = entries.get("work.progress_sweep")
        assert entry is not None, "work.progress_sweep missing from roster"
        assert isinstance(entry.schedule, EverySchedule)
        assert int(entry.schedule.interval.total_seconds()) == 300


# ---------------------------------------------------------------------------
# Sweep behaviour
# ---------------------------------------------------------------------------


class TestWorkProgressSweep:
    """The 5-min sweeper finds stuck in_progress tasks and pings their
    claimant session via the task_assignment_notify path, respecting the
    existing 30-min dedupe table."""

    def _patch_resolver_with_factory(self, monkeypatch, tmp_path, svc, store):
        """Patch the resolver so each sweep call gets a freshly-opened work
        service — matching the production loader's behaviour (the handler
        closes the service after each sweep)."""

        def _fake_loader(*, config_path=None):
            work = SQLiteWorkService(db_path=tmp_path / "work.db")
            return _RuntimeServices(
                session_service=svc,
                state_store=store,
                work_service=work,
                project_root=tmp_path,
            )

        monkeypatch.setattr(
            "pollypm.plugins_builtin.task_assignment_notify.resolver.load_runtime_services",
            _fake_loader,
        )

    def test_stuck_task_gets_resume_ping_exactly_once(
        self, tmp_path, monkeypatch,
    ):
        bus.clear_listeners()
        # Seed the work DB on disk first using a temporary connection.
        seed = SQLiteWorkService(db_path=tmp_path / "work.db")
        task_id = _claim_task(seed, "demo")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        # First sweep → fires a ping.
        result1 = work_progress_sweep_handler({})
        assert result1["outcome"] == "swept"
        assert result1["pinged"] == 1, f"expected 1 ping, got {result1!r}"
        assert len(svc.sent) == 1
        name, text = svc.sent[0]
        assert name == "worker-demo"
        assert "Resume work" in text
        assert task_id in text

        # Second sweep within the 30-min dedupe window → no re-ping.
        result2 = work_progress_sweep_handler({})
        assert result2["pinged"] == 0
        assert result2["deduped"] >= 1
        assert len(svc.sent) == 1, "sweeper must respect dedupe"

    def test_active_turn_prevents_ping(self, tmp_path, monkeypatch):
        bus.clear_listeners()
        seed = SQLiteWorkService(db_path=tmp_path / "work.db")
        _claim_task(seed, "demo")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(
            handles=[FakeHandle("worker-demo")], busy={"worker-demo"},
        )
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["skipped_active_turn"] == 1
        assert result["pinged"] == 0
        assert len(svc.sent) == 0

    def test_recent_event_prevents_ping(self, tmp_path, monkeypatch):
        """A session that recorded an event within the staleness window
        should not be flagged. Tests the false-positive guard on an
        actively-working session."""
        bus.clear_listeners()
        seed = SQLiteWorkService(db_path=tmp_path / "work.db")
        _claim_task(seed, "demo")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        # Record a fresh event for the worker — sweep must skip.
        store.record_event("worker-demo", "turn_complete", "did some work")
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["skipped_recent_event"] == 1
        assert result["pinged"] == 0
        assert len(svc.sent) == 0

    def test_stale_event_allows_ping(self, tmp_path, monkeypatch):
        """An event older than the stale threshold must not suppress."""
        bus.clear_listeners()
        seed = SQLiteWorkService(db_path=tmp_path / "work.db")
        _claim_task(seed, "demo")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(handles=[FakeHandle("worker-demo")])
        # Back-date a historical event past the threshold.
        cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        store.execute(
            "INSERT INTO events (session_name, event_type, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("worker-demo", "turn_complete", "old work", cutoff),
        )
        store.commit()
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["pinged"] == 1
        assert len(svc.sent) == 1

    def test_no_session_for_task_is_skipped(self, tmp_path, monkeypatch):
        """When no worker session exists for the task's project, the
        sweeper should skip it (no crash, no phantom send)."""
        bus.clear_listeners()
        seed = SQLiteWorkService(db_path=tmp_path / "work.db")
        _claim_task(seed, "demo")
        seed.close()

        store = StateStore(tmp_path / "state.db")
        svc = FakeSessionService(handles=[])  # nobody running
        self._patch_resolver_with_factory(monkeypatch, tmp_path, svc, store)

        result = work_progress_sweep_handler({})
        assert result["outcome"] == "swept"
        assert result["skipped_no_session"] == 1
        assert result["pinged"] == 0
        assert len(svc.sent) == 0
