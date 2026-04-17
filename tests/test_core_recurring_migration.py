"""Tests for the core_recurring + itsalive plugin roster/handler migration.

Track 7 (#164) moves recurring work from direct supervisor dispatch onto
the roster + job queue. Verifies:

* The ``core_recurring`` builtin plugin registers the expected five
  schedules + handlers at the documented cadences.
* The ``itsalive`` plugin registers its own ``deploy_sweep`` handler +
  schedule (plugin-owned, core no longer reaches into it).
* A ``HeartbeatRail`` wired from the plugin host enqueues roster entries on
  tick and drains them via registered handlers.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pollypm.heartbeat import Heartbeat, Roster
from pollypm.heartbeat.boot import HeartbeatRail, WorkerSettings, load_worker_settings
from pollypm.jobs import JobHandlerRegistry, JobQueue, JobStatus, exponential_backoff
from pollypm.plugin_api.v1 import JobHandlerAPI, RosterAPI
from pollypm.plugins_builtin.core_recurring.plugin import plugin as core_plugin
from pollypm.plugins_builtin.itsalive.plugin import plugin as itsalive_plugin


# ---------------------------------------------------------------------------
# core_recurring plugin
# ---------------------------------------------------------------------------


class TestCoreRecurringPlugin:
    def test_handler_registrations(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="core_recurring")
        assert core_plugin.register_handlers is not None
        core_plugin.register_handlers(api)

        expected = {
            "session.health_sweep",
            "capacity.probe",
            "transcript.ingest",
            "alerts.gc",
            # #249 — work-service-aware progress sweeper.
            "work.progress_sweep",
            # DB hygiene — incremental vacuum + TTL sweep for memory_entries.
            "db.vacuum",
            "memory.ttl_sweep",
        }
        assert expected.issubset(set(registry.names()))
        # inbox.sweep was retired with the legacy inbox subsystem (iv04).
        assert "inbox.sweep" not in registry.names()

    def test_roster_registrations_and_cadences(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="core_recurring")
        assert core_plugin.register_roster is not None
        core_plugin.register_roster(api)

        entries = {entry.handler_name: entry for entry in roster.entries}
        assert set(entries.keys()) == {
            "session.health_sweep",
            "capacity.probe",
            "transcript.ingest",
            "alerts.gc",
            # #249 — work-service-aware progress sweeper.
            "work.progress_sweep",
            # DB hygiene — daily cron at ~4am local.
            "db.vacuum",
            "memory.ttl_sweep",
            # Notification-staging 30-day prune — lands next to the
            # other daily cron entries around 4am.
            "notification_staging.prune",
        }

        # Cadences per issue #164 / #249.
        assert _interval_seconds(entries["session.health_sweep"]) == 10
        assert _interval_seconds(entries["capacity.probe"]) == 60
        assert _interval_seconds(entries["transcript.ingest"]) == 30
        assert _interval_seconds(entries["alerts.gc"]) == 300
        assert _interval_seconds(entries["work.progress_sweep"]) == 300
        # DB hygiene entries are 5-field cron, not @every — just verify
        # they parse into a CronSchedule with the expected expression so
        # the fleet doesn't sync on the hour.
        from pollypm.heartbeat.roster import CronSchedule

        db_vacuum = entries["db.vacuum"].schedule
        assert isinstance(db_vacuum, CronSchedule)
        assert db_vacuum.expression() == "7 4 * * *"
        mem_sweep = entries["memory.ttl_sweep"].schedule
        assert isinstance(mem_sweep, CronSchedule)
        assert mem_sweep.expression() == "13 4 * * *"
        ns_prune = entries["notification_staging.prune"].schedule
        assert isinstance(ns_prune, CronSchedule)
        assert ns_prune.expression() == "19 4 * * *"

    def test_plugin_declares_expected_capabilities(self) -> None:
        kinds = {cap.kind for cap in core_plugin.capabilities}
        assert "roster_entry" in kinds or "roster" in kinds  # legacy-compat
        assert "job_handler" in kinds


def _interval_seconds(entry) -> int:
    """Extract the EverySchedule interval in seconds from a roster entry."""
    from pollypm.heartbeat.roster import EverySchedule

    assert isinstance(entry.schedule, EverySchedule), entry
    return int(entry.schedule.interval.total_seconds())


# ---------------------------------------------------------------------------
# itsalive plugin
# ---------------------------------------------------------------------------


class TestItsaliveRosterMigration:
    def test_registers_deploy_sweep_handler(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="itsalive")
        assert itsalive_plugin.register_handlers is not None
        itsalive_plugin.register_handlers(api)
        assert "itsalive.deploy_sweep" in registry

    def test_registers_deploy_sweep_on_60s(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="itsalive")
        assert itsalive_plugin.register_roster is not None
        itsalive_plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        assert "itsalive.deploy_sweep" in entries
        assert _interval_seconds(entries["itsalive.deploy_sweep"]) == 60

    def test_itsalive_sweep_handler_is_noop_when_no_pending(self, tmp_path: Path) -> None:
        from pollypm.plugins_builtin.itsalive.plugin import itsalive_sweep_handler

        # No pending/ directory — sweep should complete with zero outcomes.
        result = itsalive_sweep_handler({"project_root": str(tmp_path)})
        assert result == {"swept": 0, "by_status": {}}


# ---------------------------------------------------------------------------
# Boot wiring (HeartbeatRail)
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal plugin-host stand-in for HeartbeatRail tests."""

    def __init__(self, roster: Roster, registry: JobHandlerRegistry) -> None:
        self._roster = roster
        self._registry = registry

    def build_roster(self) -> Roster:
        return self._roster

    def job_handler_registry(self) -> JobHandlerRegistry:
        return self._registry


class TestHeartbeatRailBoot:
    def test_from_plugin_host_wires_queue_pool_heartbeat(self, tmp_path: Path) -> None:
        roster = Roster()
        registry = JobHandlerRegistry()
        host = _FakeHost(roster, registry)

        rail = HeartbeatRail.from_plugin_host(
            state_db=tmp_path / "state.db",
            plugin_host=host,
            concurrency=1,
        )
        try:
            assert rail.queue is not None
            assert rail.pool is not None
            assert rail.heartbeat is not None
            assert rail.roster is roster
        finally:
            rail.stop(timeout=1)

    def test_tick_enqueues_and_pool_drains(self, tmp_path: Path) -> None:
        """End-to-end: roster entry fires on tick, worker runs handler."""
        seen: list[dict] = []

        registry = JobHandlerRegistry()
        registry.register(
            name="unit.ping",
            handler=lambda payload: seen.append(payload),
            plugin_name="test",
            timeout_seconds=2.0,
        )

        roster = Roster()
        # @every 1s so the first tick after first_seen_at + 1s will fire.
        roster.register(
            schedule="@on_startup",
            handler_name="unit.ping",
            payload={"hello": "world"},
        )

        host = _FakeHost(roster, registry)
        rail = HeartbeatRail.from_plugin_host(
            state_db=tmp_path / "state.db",
            plugin_host=host,
            concurrency=1,
        )
        # Tighten the queue's retry backoff so any transient failure doesn't
        # stall the test; the pool's poll_interval keeps claims snappy.
        rail.queue.retry_policy = exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0
        )
        rail.pool.poll_interval = 0.02
        rail.start()
        try:
            result = rail.tick(datetime.now(UTC))
            assert result.enqueued_count == 1
            assert _wait_until(lambda: seen, timeout=3.0)
        finally:
            rail.stop(timeout=2)

        assert seen == [{"hello": "world"}]


# ---------------------------------------------------------------------------
# WorkerSettings loader
# ---------------------------------------------------------------------------


class TestWorkerSettings:
    def test_defaults_when_config_missing(self, tmp_path: Path) -> None:
        settings = load_worker_settings(tmp_path / "does-not-exist.toml")
        assert settings.concurrency >= 1
        assert settings.poll_interval > 0

    def test_reads_heartbeat_workers_table(self, tmp_path: Path) -> None:
        path = tmp_path / "pollypm.toml"
        path.write_text(
            '[heartbeat.workers]\nconcurrency = 8\npoll_interval = 1.5\n'
        )
        settings = load_worker_settings(path)
        assert settings.concurrency == 8
        assert settings.poll_interval == 1.5

    def test_clamps_bad_values(self, tmp_path: Path) -> None:
        path = tmp_path / "pollypm.toml"
        path.write_text(
            '[heartbeat.workers]\nconcurrency = 0\npoll_interval = -1\n'
        )
        settings = load_worker_settings(path)
        # Concurrency clamped to >=1; poll_interval falls back to default.
        assert settings.concurrency == 1
        assert settings.poll_interval > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
