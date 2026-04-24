"""Tests for the HeartbeatRail background ticker thread + heartbeat CLI fallback.

Covers the fix for #268 Gap A — in production nothing was calling
``rail.tick(now)`` on a schedule, so roster-registered recurring
handlers (e.g. ``task_assignment.sweep``) never fired. Two
complementary drivers now exist:

  1. A daemon ticker thread inside ``HeartbeatRail.start()`` that calls
     ``tick()`` on a fixed interval (default 15s). Any long-lived
     process that boots the rail (cockpit TUI) gets periodic ticks for
     free.
  2. ``pm heartbeat`` as a cron-driven fallback. Even without a
     persistent host, the every-minute cron ticks the rail so the
     pipeline keeps advancing.

The ticker is deployment-time behavior — tests call ``tick()``
directly and only use the thread in lifecycle assertions (spawn,
idempotent, stop). Scheduling correctness belongs to the
heartbeat-tick tests, not here.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from pollypm.heartbeat import Roster
from pollypm.heartbeat.boot import HeartbeatRail
from pollypm.jobs import JobHandlerRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal plugin-host stand-in (mirrors test_core_recurring_migration)."""

    def __init__(self, roster: Roster, registry: JobHandlerRegistry) -> None:
        self._roster = roster
        self._registry = registry

    def build_roster(self) -> Roster:
        return self._roster

    def job_handler_registry(self) -> JobHandlerRegistry:
        return self._registry


def _build_rail(tmp_path: Path, *, tick_interval: float = 0.05) -> HeartbeatRail:
    roster = Roster()
    registry = JobHandlerRegistry()
    host = _FakeHost(roster, registry)
    return HeartbeatRail.from_plugin_host(
        state_db=tmp_path / "state.db",
        plugin_host=host,
        concurrency=1,
        tick_interval_seconds=tick_interval,
    )


# ---------------------------------------------------------------------------
# Ticker thread lifecycle
# ---------------------------------------------------------------------------


class TestTickerThreadLifecycle:
    def test_start_spawns_daemon_ticker_thread(self, tmp_path: Path) -> None:
        rail = _build_rail(tmp_path)
        try:
            rail.start()
            ticker = rail._tick_thread
            assert ticker is not None
            assert ticker.is_alive()
            assert ticker.daemon is True  # must not block interpreter exit
            assert ticker.name == "HeartbeatRail-ticker"
        finally:
            rail.stop(timeout=2)

    def test_start_is_idempotent_no_double_spawn(self, tmp_path: Path) -> None:
        rail = _build_rail(tmp_path)
        try:
            rail.start()
            first_ticker = rail._tick_thread
            assert first_ticker is not None

            # Count global HeartbeatRail-ticker threads before second start.
            names_before = [
                t.name for t in threading.enumerate()
                if t.name == "HeartbeatRail-ticker"
            ]
            rail.start()  # second call must be a no-op
            names_after = [
                t.name for t in threading.enumerate()
                if t.name == "HeartbeatRail-ticker"
            ]

            assert rail._tick_thread is first_ticker
            assert len(names_after) == len(names_before)
        finally:
            rail.stop(timeout=2)

    def test_stop_sets_event_and_thread_exits(self, tmp_path: Path) -> None:
        rail = _build_rail(tmp_path, tick_interval=0.05)
        rail.start()
        ticker = rail._tick_thread
        assert ticker is not None and ticker.is_alive()

        rail.stop(timeout=2)

        assert rail._tick_stop.is_set()
        assert not ticker.is_alive(), "ticker thread did not exit after stop()"
        assert rail._tick_thread is None

    def test_tick_exception_does_not_kill_thread(self, tmp_path: Path) -> None:
        """One bad tick must not kill the ticker — next iteration keeps going."""
        call_count = {"n": 0}

        class _FlakyRail(HeartbeatRail):
            """HeartbeatRail subclass whose first tick() raises."""

            __slots__ = ()

            def tick(self, now=None):  # type: ignore[override]
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("boom")
                return super().tick(now)

        # Build a rail and wrap it in the flaky subclass by adopting its
        # state. Easiest path: construct the underlying trio ourselves.
        roster = Roster()
        registry = JobHandlerRegistry()
        host = _FakeHost(roster, registry)
        base = HeartbeatRail.from_plugin_host(
            state_db=tmp_path / "state.db",
            plugin_host=host,
            concurrency=1,
            tick_interval_seconds=0.02,
        )
        rail = _FlakyRail(
            queue=base.queue,
            pool=base.pool,
            heartbeat=base.heartbeat,
            roster=base.roster,
            concurrency=1,
            tick_interval_seconds=0.02,
        )

        rail.start()
        try:
            # Wait until the ticker has called tick() at least twice.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and call_count["n"] < 2:
                time.sleep(0.02)
            assert call_count["n"] >= 2, (
                f"ticker died after first exception (calls={call_count['n']})"
            )
            # And the thread must still be alive.
            assert rail._tick_thread is not None
            assert rail._tick_thread.is_alive()
        finally:
            rail.stop(timeout=2)


# ---------------------------------------------------------------------------
# `pm heartbeat` CLI fallback driver
# ---------------------------------------------------------------------------


class TestHeartbeatCliFallbackTick:
    def test_tick_called_when_rail_available(self) -> None:
        """``_tick_core_rail_if_available`` drives the heartbeat rail once."""
        from pollypm.cli import _tick_core_rail_if_available

        heartbeat_rail = MagicMock()
        tick_result = object()
        heartbeat_rail.tick.return_value = tick_result
        core_rail = MagicMock()
        core_rail.get_heartbeat_rail.return_value = heartbeat_rail

        supervisor = MagicMock()
        supervisor.core_rail = core_rail

        result = _tick_core_rail_if_available(supervisor)

        core_rail.start.assert_called_once()
        heartbeat_rail.tick.assert_called_once()
        assert result is tick_result

    def test_noop_when_supervisor_has_no_core_rail(self) -> None:
        """Legacy / mocked supervisors without ``core_rail`` don't crash."""
        from pollypm.cli import _tick_core_rail_if_available

        # A supervisor without ``core_rail`` attribute at all.
        class _Legacy:
            pass

        # Should simply return without raising.
        _tick_core_rail_if_available(_Legacy())

    def test_noop_when_heartbeat_rail_not_booted(self) -> None:
        """If CoreRail.get_heartbeat_rail returns None, silently skip tick."""
        from pollypm.cli import _tick_core_rail_if_available

        core_rail = MagicMock()
        core_rail.get_heartbeat_rail.return_value = None

        supervisor = MagicMock()
        supervisor.core_rail = core_rail

        # Must not raise even though there's no heartbeat rail to tick.
        _tick_core_rail_if_available(supervisor)
        core_rail.start.assert_called_once()

    def test_tick_exception_is_swallowed(self) -> None:
        """A raising tick() must not propagate out of the CLI helper."""
        from pollypm.cli import _tick_core_rail_if_available

        heartbeat_rail = MagicMock()
        heartbeat_rail.tick.side_effect = RuntimeError("boom")
        core_rail = MagicMock()
        core_rail.get_heartbeat_rail.return_value = heartbeat_rail

        supervisor = MagicMock()
        supervisor.core_rail = core_rail

        # Must not raise — session-health sweep already succeeded upstream.
        _tick_core_rail_if_available(supervisor)
        heartbeat_rail.tick.assert_called_once()

    def test_drain_and_stop_waits_for_enqueued_jobs(self) -> None:
        from pollypm.cli import _drain_and_stop_core_rail_if_available

        class _Queue:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, job_id: int):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(status="claimed")
                return SimpleNamespace(status="done")

        heartbeat_rail = SimpleNamespace(queue=_Queue())
        core_rail = MagicMock()
        core_rail.get_heartbeat_rail.return_value = heartbeat_rail

        supervisor = MagicMock()
        supervisor.core_rail = core_rail
        tick_result = SimpleNamespace(enqueued=[SimpleNamespace(job_id=123)])

        _drain_and_stop_core_rail_if_available(
            supervisor,
            tick_result=tick_result,
            drain_timeout_seconds=0.2,
            poll_interval_seconds=0.0,
        )

        assert heartbeat_rail.queue.calls >= 2
        core_rail.stop.assert_called_once()

    def test_drain_and_stop_still_stops_without_jobs(self) -> None:
        from pollypm.cli import _drain_and_stop_core_rail_if_available

        heartbeat_rail = MagicMock()
        core_rail = MagicMock()
        core_rail.get_heartbeat_rail.return_value = heartbeat_rail

        supervisor = MagicMock()
        supervisor.core_rail = core_rail

        _drain_and_stop_core_rail_if_available(
            supervisor,
            tick_result=SimpleNamespace(enqueued=[]),
        )

        core_rail.stop.assert_called_once()
