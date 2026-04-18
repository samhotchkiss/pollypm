"""Cron-schedule dispatch tests — regression coverage for the P0 fix.

The previous ``_cron_is_due`` implementation gated dispatch on whether
``CronSchedule.next_due(first_seen_at - 1m)`` landed inside ``[…, now]``.
Because every fresh ``pm heartbeat`` invocation rebuilds the roster and
sets ``first_seen_at = now`` on the very first tick, that lookback
window collapsed to roughly the past 60 seconds. Any cron minute the
ticker overflew (busy cockpit, locked DB, restart) was lost forever —
no catch-up, no next-day fire, dead on arrival.

The fix dispatches when ``now``'s wall-clock minute matches the cron
expression (with a per-minute dedupe via ``last_fired_at``) and falls
through to the original ``next_due`` catch-up only for the long-lived
in-process ticker. These tests cover both paths plus the manifest-roster
wiring so the ``"7 4 * * *"`` style entries shipped via the core_recurring
plugin actually have a ``CronSchedule`` attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pollypm.heartbeat import Heartbeat, Roster
from pollypm.heartbeat.roster import CronSchedule, parse_schedule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeQueue:
    enqueued: list[dict[str, Any]] = field(default_factory=list)

    def enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
    ) -> int:
        self.enqueued.append(
            {
                "handler_name": handler_name,
                "payload": dict(payload),
                "dedupe_key": dedupe_key,
            }
        )
        return len(self.enqueued)


def _utc(year=2026, month=4, day=18, hour=4, minute=7, second=0, micro=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)


# ---------------------------------------------------------------------------
# CronSchedule._matches — the building block the fix relies on
# ---------------------------------------------------------------------------


class TestCronMatches:
    """``CronSchedule._matches(dt)`` is the per-minute predicate the fixed
    dispatcher consults. These tests pin its semantics so the dispatch
    behavior in the next class can rely on a stable contract.
    """

    def test_daily_match_at_exact_minute(self) -> None:
        sched = parse_schedule("7 4 * * *")
        assert isinstance(sched, CronSchedule)
        assert sched._matches(_utc(hour=4, minute=7, second=0)) is True

    def test_daily_no_match_off_by_one_minute(self) -> None:
        sched = parse_schedule("7 4 * * *")
        assert sched._matches(_utc(hour=4, minute=8, second=0)) is False

    def test_step_minute_match(self) -> None:
        sched = parse_schedule("*/5 * * * *")
        assert sched._matches(_utc(minute=0, second=0)) is True
        assert sched._matches(_utc(minute=5, second=0)) is True
        assert sched._matches(_utc(minute=55, second=0)) is True
        # Off-step minutes do not match.
        assert sched._matches(_utc(minute=1, second=0)) is False
        assert sched._matches(_utc(minute=4, second=0)) is False
        assert sched._matches(_utc(minute=6, second=0)) is False

    def test_hourly_offset_pattern(self) -> None:
        # The shipped cron strings use hourly-at-minute-N — make sure each
        # of the three patterns we now ship matches at its scheduled minute
        # and at no other.
        for minute_of_hour, expr in [
            (23, "23 * * * *"),
            (31, "31 * * * *"),
            (37, "37 * * * *"),
        ]:
            sched = parse_schedule(expr)
            assert sched._matches(_utc(minute=minute_of_hour, second=0)) is True
            assert sched._matches(_utc(minute=(minute_of_hour + 1) % 60)) is False


# ---------------------------------------------------------------------------
# Heartbeat tick — the real fix (does dispatch land?)
# ---------------------------------------------------------------------------


class TestCronDispatch:
    def test_daily_cron_fires_when_tick_lands_in_matching_minute(self) -> None:
        """The flagship case: ``7 4 * * *`` must fire when the ticker is
        running at 04:07:xx. Pre-fix, this only fired when ``last_fired_at``
        already lay just before 04:07 — never on the first tick of a fresh
        process — which is why the audit found zero events for nine days.
        """
        roster = Roster()
        roster.register(
            schedule="7 4 * * *",
            handler_name="db.vacuum",
            dedupe_key="db.vacuum",
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # Tick at 04:07:00 exactly — fresh process, last_fired_at=None.
        result = hb.tick(_utc(hour=4, minute=7, second=0))
        assert result.enqueued_count == 1
        assert queue.enqueued[0]["handler_name"] == "db.vacuum"

    def test_15s_ticker_catches_cron_minute_even_off_the_zero_second(self) -> None:
        """The default rail tick interval is 15 seconds, which means we
        almost never tick exactly at ``:00`` of the matching minute. The
        previous implementation could fly past the scheduled fire if the
        only tick that landed inside the matching minute happened mid-
        minute and the ``next_due`` lookback rounded the wrong way.
        """
        roster = Roster()
        roster.register(
            schedule="7 4 * * *",
            handler_name="db.vacuum",
            dedupe_key="db.vacuum",
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # Walk a 15s ticker across the matching minute. The boot tick
        # lands at 04:06:55 (one tick before the matching minute), then
        # every 15 seconds. The first tick inside ``04:07`` is at
        # 04:07:10 — well off the ``:00`` boundary that ``next_due`` would
        # have rounded forward past in the broken implementation.
        ticks = [
            _utc(hour=4, minute=6, second=55),  # boot tick — no fire
            _utc(hour=4, minute=7, second=10),  # first matching-minute tick
            _utc(hour=4, minute=7, second=25),
            _utc(hour=4, minute=7, second=40),
            _utc(hour=4, minute=7, second=55),
            _utc(hour=4, minute=8, second=10),  # past the minute — no extra fire
        ]
        for tick_at in ticks:
            hb.tick(tick_at)

        # Exactly one fire across the matching minute.
        assert len(queue.enqueued) == 1, queue.enqueued

    def test_no_double_fire_within_same_minute(self) -> None:
        """Two ticks inside the matching minute must NOT enqueue twice.
        ``last_fired_at`` carries the per-minute dedupe inside one process.
        """
        roster = Roster()
        roster.register(
            schedule="7 4 * * *",
            handler_name="db.vacuum",
            dedupe_key="db.vacuum",
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        hb.tick(_utc(hour=4, minute=7, second=0))
        hb.tick(_utc(hour=4, minute=7, second=15))
        hb.tick(_utc(hour=4, minute=7, second=30))
        hb.tick(_utc(hour=4, minute=7, second=45))

        assert len(queue.enqueued) == 1

    def test_no_fire_on_non_matching_minute(self) -> None:
        """A daily cron at 04:07 must stay silent during the rest of the day."""
        roster = Roster()
        roster.register(
            schedule="7 4 * * *",
            handler_name="db.vacuum",
            dedupe_key="db.vacuum",
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        for hour in (0, 3, 5, 12, 23):
            hb.tick(_utc(hour=hour, minute=7, second=0))
        for minute in (0, 6, 8, 30, 59):
            if minute == 7:
                continue
            hb.tick(_utc(hour=4, minute=minute, second=0))

        assert queue.enqueued == []

    def test_hourly_offset_cron_fires_each_hour(self) -> None:
        """Each shipped hourly cron (``23``/``31``/``37``) must fire at its
        scheduled minute every hour, once per hour, regardless of which
        second the ticker happens to land on.
        """
        for expr, handler in [
            ("23 * * * *", "agent_worktree.prune"),
            ("31 * * * *", "log.rotate"),
            ("37 * * * *", "events.retention_sweep"),
        ]:
            roster = Roster()
            roster.register(schedule=expr, handler_name=handler, dedupe_key=handler)
            queue = FakeQueue()
            hb = Heartbeat(roster, queue)

            # Walk 3 hours of 15s ticks.
            now = _utc(hour=0, minute=0, second=0)
            end = now + timedelta(hours=3)
            while now < end:
                hb.tick(now)
                now += timedelta(seconds=15)

            # Should have fired exactly 3 times — once per hour.
            assert len(queue.enqueued) == 3, (
                f"{expr}: got {len(queue.enqueued)} fires, expected 3"
            )

    def test_cron_fires_under_simulated_external_pm_heartbeat(self) -> None:
        """Production case: ``pm heartbeat`` invoked once per minute by the
        OS cron, each invocation a brand-new process with a brand-new
        Roster (``last_fired_at == None`` always). The fix must dispatch
        for crontab patterns under this every-minute fresh-process cadence.
        """
        # 2 hours of one-fresh-roster-per-minute heartbeats — what the OS
        # cron pumps out at ``* * * * *``.
        fires_db_vacuum = 0
        fires_log_rotate = 0
        fires_events_retention = 0
        fires_agent_worktree = 0

        start = datetime(2026, 4, 18, 3, 0, 0, tzinfo=UTC)
        end = datetime(2026, 4, 18, 5, 0, 0, tzinfo=UTC)
        now = start
        while now < end:
            # Fresh process every minute — fresh roster, no carried state.
            roster = Roster()
            roster.register(
                schedule="7 4 * * *",
                handler_name="db.vacuum",
                dedupe_key="db.vacuum",
            )
            roster.register(
                schedule="31 * * * *",
                handler_name="log.rotate",
                dedupe_key="log.rotate",
            )
            roster.register(
                schedule="37 * * * *",
                handler_name="events.retention_sweep",
                dedupe_key="events.retention_sweep",
            )
            roster.register(
                schedule="23 * * * *",
                handler_name="agent_worktree.prune",
                dedupe_key="agent_worktree.prune",
            )
            queue = FakeQueue()
            hb = Heartbeat(roster, queue)
            # Cron typically fires within ~1s of the minute boundary.
            hb.tick(now + timedelta(milliseconds=350))
            for job in queue.enqueued:
                if job["handler_name"] == "db.vacuum":
                    fires_db_vacuum += 1
                elif job["handler_name"] == "log.rotate":
                    fires_log_rotate += 1
                elif job["handler_name"] == "events.retention_sweep":
                    fires_events_retention += 1
                elif job["handler_name"] == "agent_worktree.prune":
                    fires_agent_worktree += 1
            now += timedelta(minutes=1)

        # 04:07 happens once in [03:00, 05:00).
        assert fires_db_vacuum == 1
        # 31 / 37 each happen twice (03:31, 04:31 / 03:37, 04:37).
        assert fires_log_rotate == 2
        assert fires_events_retention == 2
        # 23 happens twice (03:23, 04:23).
        assert fires_agent_worktree == 2

    def test_in_process_catch_up_after_skipped_ticks(self) -> None:
        """Long-lived rail: if the ticker stalls for several minutes and
        misses the cron's scheduled minute entirely, the next tick should
        still enqueue once (the original catch-up-once contract).
        """
        roster = Roster()
        roster.register(
            schedule="23 * * * *",
            handler_name="agent_worktree.prune",
            dedupe_key="agent_worktree.prune",
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # Bootstrap at 03:20 — sets last_fired_at by firing once on the
        # 03:23 minute, then we'll simulate a stall past 04:23.
        hb.tick(_utc(hour=3, minute=20, second=0))
        hb.tick(_utc(hour=3, minute=23, second=5))
        assert len(queue.enqueued) == 1
        # Ticker stalls; next tick is at 04:30 — well past the 04:23 fire.
        hb.tick(_utc(hour=4, minute=30, second=0))
        # Catch-up-once: exactly one extra fire for the missed 04:23.
        assert len(queue.enqueued) == 2


# ---------------------------------------------------------------------------
# Plugin-manifest wiring — the roster entries must actually have a schedule
# ---------------------------------------------------------------------------


class TestCoreRecurringRosterAttachment:
    """Verify that the core_recurring plugin's ``register_roster`` hook
    produces ``RosterEntry`` objects whose ``schedule`` is a
    ``CronSchedule`` (not ``None``, not a stringly-typed placeholder) for
    every shipped crontab handler.

    Regression for the third candidate root cause from the bug report:
    "the roster loader doesn't actually attach a CronSchedule to roster
    entries — leaves schedule=None, which dispatches never".
    """

    def test_shipped_cron_entries_have_cron_schedules(self) -> None:
        from pollypm.heartbeat.roster import CronSchedule
        from pollypm.plugin_api.v1 import RosterAPI
        from pollypm.plugins_builtin.core_recurring.plugin import _register_roster

        roster = Roster()
        api = RosterAPI(roster, plugin_name="core_recurring")
        _register_roster(api)

        # Build a {handler_name: schedule} index from the live roster.
        by_handler: dict[str, Any] = {
            entry.handler_name: entry.schedule for entry in roster.entries
        }

        expected_cron_handlers = {
            "db.vacuum",
            "memory.ttl_sweep",
            "events.retention_sweep",
            "notification_staging.prune",
            "agent_worktree.prune",
            "log.rotate",
        }
        for handler in expected_cron_handlers:
            assert handler in by_handler, f"missing roster entry for {handler}"
            sched = by_handler[handler]
            assert isinstance(sched, CronSchedule), (
                f"{handler} schedule is {type(sched).__name__}, expected CronSchedule"
            )

    def test_db_vacuum_cron_string_round_trips(self) -> None:
        """Smoke test: parse the exact ``"7 4 * * *"`` string the plugin
        registers and confirm it agrees with the dispatcher about what
        04:07 means.
        """
        from pollypm.heartbeat.roster import CronSchedule, parse_schedule

        sched = parse_schedule("7 4 * * *")
        assert isinstance(sched, CronSchedule)
        assert sched._matches(_utc(hour=4, minute=7, second=0)) is True
        assert sched._matches(_utc(hour=4, minute=7, second=59)) is True  # second-agnostic
        assert sched._matches(_utc(hour=4, minute=6, second=59)) is False
        assert sched._matches(_utc(hour=4, minute=8, second=0)) is False


# ---------------------------------------------------------------------------
# Pre-fix regression assertion — kept simple to make the bug obvious if a
# future change re-introduces the lookback-window failure.
# ---------------------------------------------------------------------------


def test_cron_fires_when_first_seen_at_equals_now() -> None:
    """The exact trigger geometry that broke production: the first tick
    of a fresh roster, where ``first_seen_at`` will be set to the same
    timestamp the dispatcher then evaluates. With the old lookback-window
    logic this still worked when ``next_due(now - 1m)`` happened to land
    inside the matching minute — but only just. The new wall-clock-minute
    check makes the success unconditional.
    """
    roster = Roster()
    roster.register(
        schedule="7 4 * * *",
        handler_name="db.vacuum",
        dedupe_key="db.vacuum",
    )
    queue = FakeQueue()
    hb = Heartbeat(roster, queue)

    # No first_seen_at pre-set — tick will set it to ``now``.
    result = hb.tick(_utc(hour=4, minute=7, second=42))
    assert result.enqueued_count == 1
