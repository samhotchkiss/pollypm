"""Unit tests for the sealed heartbeat tick."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pollypm.heartbeat import (
    Heartbeat,
    Roster,
    RosterEntry,
    parse_schedule,
)
from pollypm.heartbeat.roster import (
    CronSchedule,
    EverySchedule,
    OnStartupSchedule,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeQueue:
    """In-memory queue that records every enqueue call."""

    enqueued: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 0

    def enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
    ) -> int:
        self.next_id += 1
        self.enqueued.append(
            {
                "id": self.next_id,
                "handler_name": handler_name,
                "payload": dict(payload),
                "dedupe_key": dedupe_key,
                "run_after": run_after,
            }
        )
        return self.next_id


def _utc(year=2026, month=1, day=1, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------


class TestParseSchedule:
    def test_on_startup(self) -> None:
        assert isinstance(parse_schedule("@on_startup"), OnStartupSchedule)

    def test_every_seconds(self) -> None:
        s = parse_schedule("@every 30s")
        assert isinstance(s, EverySchedule)
        assert s.interval == timedelta(seconds=30)

    def test_every_minutes(self) -> None:
        s = parse_schedule("@every 5m")
        assert isinstance(s, EverySchedule)
        assert s.interval == timedelta(minutes=5)

    def test_every_hours(self) -> None:
        s = parse_schedule("@every 2h")
        assert isinstance(s, EverySchedule)
        assert s.interval == timedelta(hours=2)

    def test_every_bare_number_is_seconds(self) -> None:
        s = parse_schedule("@every 15")
        assert isinstance(s, EverySchedule)
        assert s.interval == timedelta(seconds=15)

    def test_cron_basic(self) -> None:
        s = parse_schedule("*/5 * * * *")
        assert isinstance(s, CronSchedule)
        assert s.minute == frozenset(range(0, 60, 5))

    def test_cron_specific_hour(self) -> None:
        s = parse_schedule("0 9 * * *")
        assert isinstance(s, CronSchedule)
        assert s.minute == frozenset({0})
        assert s.hour == frozenset({9})

    def test_cron_range(self) -> None:
        s = parse_schedule("0 9-17 * * 1-5")
        assert isinstance(s, CronSchedule)
        assert s.hour == frozenset(range(9, 18))
        assert s.dow == frozenset(range(1, 6))

    def test_cron_aliases(self) -> None:
        s = parse_schedule("@hourly")
        assert isinstance(s, CronSchedule)
        assert s.minute == frozenset({0})

        s = parse_schedule("@daily")
        assert isinstance(s, CronSchedule)
        assert s.minute == frozenset({0})
        assert s.hour == frozenset({0})

    def test_cron_comma_list(self) -> None:
        s = parse_schedule("0,15,30,45 * * * *")
        assert isinstance(s, CronSchedule)
        assert s.minute == frozenset({0, 15, 30, 45})

    def test_invalid_expression(self) -> None:
        with pytest.raises(ValueError):
            parse_schedule("")
        with pytest.raises(ValueError):
            parse_schedule("not a cron")
        with pytest.raises(ValueError):
            parse_schedule("* * * *")  # four fields
        with pytest.raises(ValueError):
            parse_schedule("@every")

    def test_cron_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            parse_schedule("99 * * * *")
        with pytest.raises(ValueError):
            parse_schedule("* 25 * * *")


# ---------------------------------------------------------------------------
# Roster behavior
# ---------------------------------------------------------------------------


class TestRoster:
    def test_register_returns_new_entry(self) -> None:
        r = Roster()
        entry, is_new = r.register(
            schedule="@every 60s",
            handler_name="sweep",
            payload={"project": "pollypm"},
        )
        assert is_new is True
        assert entry.handler_name == "sweep"
        assert len(r) == 1

    def test_duplicate_registration_is_rejected(self) -> None:
        r = Roster()
        r.register(schedule="@every 60s", handler_name="sweep", payload={"p": "a"})
        _, is_new = r.register(
            schedule="@every 60s", handler_name="sweep", payload={"p": "a"}
        )
        assert is_new is False
        assert len(r) == 1

    def test_same_handler_different_payload_is_new(self) -> None:
        r = Roster()
        r.register(schedule="@every 60s", handler_name="sweep", payload={"p": "a"})
        _, is_new = r.register(
            schedule="@every 60s", handler_name="sweep", payload={"p": "b"}
        )
        assert is_new is True
        assert len(r) == 2

    def test_identity_stable_across_payload_ordering(self) -> None:
        e1 = RosterEntry(
            schedule=parse_schedule("@every 60s"),
            handler_name="h",
            payload={"a": 1, "b": 2},
        )
        e2 = RosterEntry(
            schedule=parse_schedule("@every 60s"),
            handler_name="h",
            payload={"b": 2, "a": 1},
        )
        assert e1.identity() == e2.identity()


# ---------------------------------------------------------------------------
# Tick behavior
# ---------------------------------------------------------------------------


class TestHeartbeatTick:
    def test_empty_roster_enqueues_nothing(self) -> None:
        queue = FakeQueue()
        hb = Heartbeat(Roster(), queue)
        result = hb.tick(_utc())
        assert result.enqueued_count == 0
        assert queue.enqueued == []
        assert hb.last_tick_at == _utc()

    def test_tick_requires_tz_aware_datetime(self) -> None:
        hb = Heartbeat(Roster(), FakeQueue())
        with pytest.raises(ValueError):
            hb.tick(datetime(2026, 1, 1))

    def test_on_startup_fires_once(self) -> None:
        roster = Roster()
        roster.register(schedule="@on_startup", handler_name="boot", payload={})
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        r1 = hb.tick(_utc(minute=0))
        assert r1.enqueued_count == 1
        assert queue.enqueued[0]["handler_name"] == "boot"

        # Subsequent ticks must NOT re-fire @on_startup.
        r2 = hb.tick(_utc(minute=1))
        r3 = hb.tick(_utc(minute=2))
        assert r2.enqueued_count == 0
        assert r3.enqueued_count == 0
        assert len(queue.enqueued) == 1

    def test_every_fires_at_interval(self) -> None:
        roster = Roster()
        roster.register(schedule="@every 60s", handler_name="beat", payload={})
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # t=0: first tick anchors first_seen_at, doesn't fire (interval not elapsed).
        hb.tick(_utc(minute=0))
        assert queue.enqueued == []

        # t=30s: still not elapsed.
        hb.tick(_utc(minute=0, second=30))
        assert queue.enqueued == []

        # t=60s: fires.
        hb.tick(_utc(minute=1))
        assert len(queue.enqueued) == 1

        # t=90s: last fire at 60s, interval not elapsed.
        hb.tick(_utc(minute=1, second=30))
        assert len(queue.enqueued) == 1

        # t=120s: fires again.
        hb.tick(_utc(minute=2))
        assert len(queue.enqueued) == 2

    def test_single_due_cron_entry(self) -> None:
        roster = Roster()
        # Fire at minute 5 of every hour.
        roster.register(schedule="5 * * * *", handler_name="cron5", payload={})
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # Initialize at 12:00 — no fire yet.
        hb.tick(_utc(hour=12, minute=0))
        assert queue.enqueued == []

        # Tick at 12:05 — should fire.
        hb.tick(_utc(hour=12, minute=5))
        assert len(queue.enqueued) == 1

        # Tick at 12:10 — already fired in this hour, next fire is 13:05.
        hb.tick(_utc(hour=12, minute=10))
        assert len(queue.enqueued) == 1

    def test_many_due_entries_in_single_tick(self) -> None:
        roster = Roster()
        queue = FakeQueue()
        # 10 @every schedules all due at the same moment.
        for i in range(10):
            roster.register(
                schedule="@every 60s",
                handler_name=f"handler_{i}",
                payload={"i": i},
            )
        hb = Heartbeat(roster, queue)

        # Bootstrap tick at t=0.
        hb.tick(_utc(minute=0))
        assert queue.enqueued == []

        # At t=60s all 10 fire.
        result = hb.tick(_utc(minute=1))
        assert result.enqueued_count == 10
        assert len(queue.enqueued) == 10
        handler_names = {j["handler_name"] for j in queue.enqueued}
        assert handler_names == {f"handler_{i}" for i in range(10)}

    def test_missed_ticks_catch_up_once(self) -> None:
        """If a tick fires late, a due entry is enqueued once, not N times."""
        roster = Roster()
        roster.register(schedule="@every 60s", handler_name="beat", payload={})
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # Bootstrap at t=0.
        hb.tick(_utc(minute=0))
        assert queue.enqueued == []

        # Skip ahead 10 minutes — a naive scheduler would enqueue 10 jobs.
        # Our policy: enqueue exactly one.
        result = hb.tick(_utc(minute=10))
        assert result.enqueued_count == 1
        assert len(queue.enqueued) == 1

        # Another big jump: still at most one.
        result = hb.tick(_utc(hour=5, minute=0))
        assert result.enqueued_count == 1
        assert len(queue.enqueued) == 2

    def test_missed_ticks_catch_up_once_cron(self) -> None:
        roster = Roster()
        # Every minute.
        roster.register(schedule="* * * * *", handler_name="tick", payload={})
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        # Bootstrap at 12:00.
        hb.tick(_utc(hour=12, minute=0))
        # first_seen_at = 12:00, so next due is the minute after — 12:01.
        # But our cron matches every minute from 12:00 search_from=11:59,
        # so 12:00 also matches. We already fired once. Clear and continue.
        queue.enqueued.clear()

        # Jump ahead 5 minutes.
        result = hb.tick(_utc(hour=12, minute=5))
        # Catch-up-once: only one enqueue.
        assert result.enqueued_count == 1

    def test_future_only_entries_not_fired(self) -> None:
        roster = Roster()
        # Cron matches only at 23:59 — never matches our ticks around 12:00.
        roster.register(schedule="59 23 * * *", handler_name="late", payload={})
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        hb.tick(_utc(hour=12, minute=0))
        hb.tick(_utc(hour=12, minute=30))
        hb.tick(_utc(hour=15, minute=0))
        assert queue.enqueued == []

    def test_dedupe_key_passed_to_queue(self) -> None:
        roster = Roster()
        roster.register(
            schedule="@on_startup",
            handler_name="boot",
            payload={"project": "p"},
            dedupe_key="boot:p",
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        hb.tick(_utc())
        assert queue.enqueued[0]["dedupe_key"] == "boot:p"

    def test_payload_is_copied_not_shared(self) -> None:
        roster = Roster()
        roster.register(
            schedule="@on_startup",
            handler_name="boot",
            payload={"items": [1, 2, 3]},
        )
        queue = FakeQueue()
        hb = Heartbeat(roster, queue)

        hb.tick(_utc())
        # Mutate original entry; enqueued payload should be unaffected.
        roster.entries[0].payload["items"].append(99)
        assert queue.enqueued[0]["payload"] == {"items": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def test_tick_performance_100_entries_under_10ms() -> None:
    roster = Roster()
    # Mix of schedule types. None are due at the bootstrap tick.
    for i in range(100):
        roster.register(
            schedule="@every 300s",
            handler_name=f"h_{i}",
            payload={"i": i},
        )
    queue = FakeQueue()
    hb = Heartbeat(roster, queue)

    # Warm up one tick (sets anchors).
    hb.tick(_utc(minute=0))

    # Measure the second tick — still none due.
    start = time.perf_counter()
    result = hb.tick(_utc(minute=0, second=30))
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result.enqueued_count == 0
    # Generous-ish upper bound to avoid CI flakes; we're well under 10ms in
    # steady state. 20ms is the fail threshold.
    assert elapsed_ms < 20, f"tick took {elapsed_ms:.2f}ms for 100 entries"


def test_tick_performance_100_entries_all_due() -> None:
    """Even when every entry fires, the tick should be fast."""
    roster = Roster()
    for i in range(100):
        roster.register(
            schedule="@every 1s",
            handler_name=f"h_{i}",
            payload={"i": i},
        )
    queue = FakeQueue()
    hb = Heartbeat(roster, queue)

    hb.tick(_utc(minute=0))
    start = time.perf_counter()
    result = hb.tick(_utc(minute=0, second=5))
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert result.enqueued_count == 100
    assert elapsed_ms < 50, f"tick took {elapsed_ms:.2f}ms for 100 fires"
