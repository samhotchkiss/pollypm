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
            "account.usage_refresh",
            "transcript.ingest",
            "alerts.gc",
            # #249 — work-service-aware progress sweeper.
            "work.progress_sweep",
            # DB hygiene — incremental vacuum + TTL sweep for memory_entries.
            "db.vacuum",
            "memory.ttl_sweep",
            # #251 — worker-worktree state audit.
            "worktree.state_audit",
            # #1049 — periodic stuck-claim recovery.
            "stuck_claims.sweep",
            # #1073 — auto-escalate blocked-chain dead ends.
            "blocked_chain.sweep",
            # #savethenovel followup — audit-log watchdog (orphan
            # markers, stuck drafts, cancellation gaps).
            "audit.watchdog",
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
            "account.usage_refresh",
            "transcript.ingest",
            "alerts.gc",
            # #249 — work-service-aware progress sweeper.
            "work.progress_sweep",
            # #250 — pane-text semantic classifier.
            "pane.classify",
            # DB hygiene — daily cron at ~4am local.
            "db.vacuum",
            "memory.ttl_sweep",
            # Notification-staging 30-day prune — lands next to the
            # other daily cron entries around 4am.
            "notification_staging.prune",
            # Harness agent-worktree hygiene — hourly at minute :23.
            "agent_worktree.prune",
            # Log-file hygiene — hourly at minute :31.
            "log.rotate",
            # Events-table tiered retention — hourly at minute :37.
            "events.retention_sweep",
            # #251 — worker-worktree state audit, @every 10m.
            "worktree.state_audit",
            # #1049 — stuck-claim recovery, @every 5m.
            "stuck_claims.sweep",
            # #1073 — blocked-chain dead-end escalation, @every 10m.
            "blocked_chain.sweep",
            # #savethenovel followup — audit-log watchdog, @every 5m.
            "audit.watchdog",
        }

        # Cadences per issue #164 / #249.
        assert _interval_seconds(entries["session.health_sweep"]) == 10
        assert _interval_seconds(entries["capacity.probe"]) == 60
        assert _interval_seconds(entries["account.usage_refresh"]) == 300
        # Transcript ingestion was widened to 5m after the 30s cadence
        # proved too expensive on large roots.
        assert _interval_seconds(entries["transcript.ingest"]) == 300
        assert _interval_seconds(entries["alerts.gc"]) == 300
        assert _interval_seconds(entries["work.progress_sweep"]) == 300
        # #250 — pane-text classifier runs every 30s.
        assert _interval_seconds(entries["pane.classify"]) == 30
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
        agent_prune = entries["agent_worktree.prune"].schedule
        assert isinstance(agent_prune, CronSchedule)
        assert agent_prune.expression() == "23 * * * *"
        log_rotate = entries["log.rotate"].schedule
        assert isinstance(log_rotate, CronSchedule)
        assert log_rotate.expression() == "31 * * * *"
        events_retention = entries["events.retention_sweep"].schedule
        assert isinstance(events_retention, CronSchedule)
        assert events_retention.expression() == "37 * * * *"
        # #251 — worktree state audit uses EverySchedule(10m).
        assert _interval_seconds(entries["worktree.state_audit"]) == 600
        # #1049 — stuck-claim recovery sweep, @every 5m.
        assert _interval_seconds(entries["stuck_claims.sweep"]) == 300
        # #1073 — blocked-chain escalation sweep, @every 10m.
        assert _interval_seconds(entries["blocked_chain.sweep"]) == 600
        # #savethenovel followup — audit-log watchdog, @every 5m.
        assert _interval_seconds(entries["audit.watchdog"]) == 300

    def test_plugin_declares_expected_capabilities(self) -> None:
        kinds = {cap.kind for cap in core_plugin.capabilities}
        assert "roster_entry" in kinds or "roster" in kinds  # legacy-compat
        assert "job_handler" in kinds

    def test_split_modules_back_the_plugin_surface(self) -> None:
        from pollypm.plugins_builtin.core_recurring import maintenance, shared, sweeps
        from pollypm.plugins_builtin.core_recurring.plugin import (
            _ephemeral_alert_type,
            is_ephemeral_session_name,
            log_rotate_handler,
            sweep_ephemeral_sessions,
            work_progress_sweep_handler,
        )

        assert shared.sweep_ephemeral_sessions is sweep_ephemeral_sessions
        assert shared.is_ephemeral_session_name is is_ephemeral_session_name
        assert maintenance.log_rotate_handler is log_rotate_handler
        assert sweeps.work_progress_sweep_handler is work_progress_sweep_handler
        assert _ephemeral_alert_type("task-demo-1", "missing_window").startswith(
            "ephemeral_session_dead:"
        )

    def test_plugin_loads_through_extension_host(self, tmp_path: Path) -> None:
        """Regression for #957.

        ``ExtensionHost`` loads each plugin module via
        ``importlib.util.spec_from_file_location`` with a synthetic name —
        which has no parent package, so any ``from .x import y`` inside the
        plugin entry-point module silently fails to load. When that happens
        the plugin is dropped from the registry (the error stays on
        ``host.errors`` but is otherwise non-fatal), and the recurring
        handlers it owned — including ``account.usage_refresh`` — never run,
        leaving the cockpit's account-usage panel blank.

        Pin the loader path so reintroducing a relative import in
        ``core_recurring/plugin.py`` fails this test instead of degrading
        the cockpit silently.
        """
        from pollypm.plugin_host import ExtensionHost

        host = ExtensionHost(tmp_path)
        plugins = host.plugins()
        assert "core_recurring" in plugins, host.errors
        roster = host.build_roster()
        handler_names = {entry.handler_name for entry in roster.entries}
        assert "account.usage_refresh" in handler_names
        assert "capacity.probe" in handler_names


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
# #1052 — dedupe_key on high-cadence sweep handlers + backlog purge
# ---------------------------------------------------------------------------


class TestCadenceDedupeKeys:
    """Every parameter-free recurring sweep must register with a
    ``dedupe_key`` so a slow worker pool can't accumulate identical
    queued rows (issue #1052).
    """

    _EXPECTED_DEDUPED = {
        "session.health_sweep",
        "capacity.probe",
        "account.usage_refresh",
        "transcript.ingest",
        "alerts.gc",
        "work.progress_sweep",
        "pane.classify",
        "worktree.state_audit",
        "stuck_claims.sweep",
        "db.vacuum",
        "memory.ttl_sweep",
        "events.retention_sweep",
        "notification_staging.prune",
        "agent_worktree.prune",
        "log.rotate",
    }

    def test_core_recurring_entries_have_dedupe_keys(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="core_recurring")
        assert core_plugin.register_roster is not None
        core_plugin.register_roster(api)

        entries = {entry.handler_name: entry for entry in roster.entries}
        for handler in self._EXPECTED_DEDUPED:
            assert handler in entries, handler
            assert entries[handler].dedupe_key == handler, (
                f"{handler} missing or mismatched dedupe_key: "
                f"{entries[handler].dedupe_key!r}"
            )

    def test_task_assignment_sweep_has_dedupe_key(self) -> None:
        from pollypm.plugins_builtin.task_assignment_notify.plugin import (
            plugin as ta_plugin,
        )

        roster = Roster()
        api = RosterAPI(roster, plugin_name="task_assignment_notify")
        assert ta_plugin.register_roster is not None
        ta_plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        assert entries["task_assignment.sweep"].dedupe_key == "task_assignment.sweep"

    def test_itsalive_deploy_sweep_has_dedupe_key(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="itsalive")
        assert itsalive_plugin.register_roster is not None
        itsalive_plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        assert entries["itsalive.deploy_sweep"].dedupe_key == "itsalive.deploy_sweep"

    def test_every_production_roster_entry_has_dedupe_key(
        self, tmp_path: Path,
    ) -> None:
        """#1065 — guard against any cadence handler regressing to
        ``dedupe_key=None``.

        Per-handler tests above only cover the handlers we authored
        explicitly. This walks the full plugin host (every builtin's
        ``register_roster`` + ``initialize`` hook) and asserts every
        registered roster entry carries a ``dedupe_key``. Any future
        ``register_recurring`` callsite that forgets the kwarg fails
        here — catching the gap that #1065 reported as a stale-daemon
        symptom and matching the post-#1052 invariant: cadence-fired
        handlers with empty/identical payloads must coalesce on
        ``handler_name``.
        """
        from pollypm.plugin_host import extension_host_for_root

        host = extension_host_for_root(str(tmp_path))
        roster = host.build_roster()
        host.initialize_plugins(
            roster=roster, job_registry=host.job_handler_registry(),
        )

        missing = [
            entry.handler_name
            for entry in roster.entries
            if entry.dedupe_key is None
        ]
        assert not missing, (
            "Roster entries without dedupe_key (cadence ticks will "
            "compound under contention — see #1052/#1065): "
            f"{missing}"
        )


class TestStaleCadencePurge:
    """The alerts.gc handler drains the legacy un-keyed backlog (#1052)."""

    def test_purge_drops_stale_queued_rows_for_deduped_handlers(
        self, tmp_path: Path,
    ) -> None:
        from pollypm.jobs import JobQueue
        from pollypm.plugins_builtin.core_recurring.plugin import (
            _DEDUPED_CADENCE_HANDLERS,
            _STALE_QUEUED_CUTOFF_SECONDS,
            _prune_stale_cadence_jobs,
        )

        db_path = tmp_path / "state.db"
        # Seed: stale queued rows for every deduped handler, plus a fresh
        # row that must survive, plus a stale-but-not-queued row that
        # must survive (terminal-state rows are untouched).
        old_iso = (
            datetime.now(UTC)
            - timedelta(seconds=_STALE_QUEUED_CUTOFF_SECONDS + 600)
        ).isoformat()
        fresh_iso = datetime.now(UTC).isoformat()

        with JobQueue(db_path=db_path) as q:
            cur = q._conn  # noqa: SLF001 — direct seed for the maintenance test
            for handler in _DEDUPED_CADENCE_HANDLERS:
                cur.execute(
                    """
                    INSERT INTO work_jobs (
                        handler_name, payload_json, status,
                        enqueued_at, run_after
                    ) VALUES (?, '{}', 'queued', ?, ?)
                    """,
                    (handler, old_iso, old_iso),
                )
            # Fresh queued row for one of the handlers — must survive.
            cur.execute(
                """
                INSERT INTO work_jobs (
                    handler_name, payload_json, status,
                    enqueued_at, run_after
                ) VALUES ('session.health_sweep', '{}', 'queued', ?, ?)
                """,
                (fresh_iso, fresh_iso),
            )
            # Stale claimed row — must survive (only queued rows are
            # purged so we don't yank work in flight).
            cur.execute(
                """
                INSERT INTO work_jobs (
                    handler_name, payload_json, status,
                    enqueued_at, run_after, claimed_at, claimed_by
                ) VALUES (
                    'session.health_sweep', '{}', 'claimed',
                    ?, ?, ?, 'worker-1'
                )
                """,
                (old_iso, old_iso, old_iso),
            )
            # Stale queued row for an *unknown* handler — must survive
            # (we only sweep handlers we authored a dedupe_key for).
            cur.execute(
                """
                INSERT INTO work_jobs (
                    handler_name, payload_json, status,
                    enqueued_at, run_after
                ) VALUES ('user.custom_handler', '{}', 'queued', ?, ?)
                """,
                (old_iso, old_iso),
            )

        deleted = _prune_stale_cadence_jobs(db_path)
        assert deleted == len(_DEDUPED_CADENCE_HANDLERS)

        # Survivors: 1 fresh + 1 claimed + 1 unknown handler row = 3.
        with JobQueue(db_path=db_path) as q:
            survivors = q._conn.execute(  # noqa: SLF001
                "SELECT handler_name, status FROM work_jobs "
                "ORDER BY id ASC",
            ).fetchall()
        assert len(survivors) == 3
        kinds = {(row[0], row[1]) for row in survivors}
        assert ("session.health_sweep", "queued") in kinds
        assert ("session.health_sweep", "claimed") in kinds
        assert ("user.custom_handler", "queued") in kinds

    def test_purge_is_noop_on_empty_db(self, tmp_path: Path) -> None:
        from pollypm.plugins_builtin.core_recurring.plugin import (
            _prune_stale_cadence_jobs,
        )

        db_path = tmp_path / "state.db"
        # Force schema creation by opening + closing the queue once.
        from pollypm.jobs import JobQueue

        with JobQueue(db_path=db_path):
            pass

        assert _prune_stale_cadence_jobs(db_path) == 0


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
