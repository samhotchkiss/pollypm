"""The sealed heartbeat tick.

Reads current time, consults the roster, enqueues jobs for due entries.
That's it. No side effects beyond ``JobQueue.enqueue`` calls and updating
``RosterEntry.last_fired_at`` / ``Heartbeat.last_tick_at`` bookkeeping.

Overdue policy
--------------
If the tick fires late (e.g. the supervisor slept past a scheduled minute),
each due entry is enqueued **at most once per tick** — we never fan out a
backlog of missed fires. Rationale:

* Most handlers are either idempotent sweeps or use ``dedupe_key`` to
  suppress pileups; replaying N missed fires provides no benefit.
* A runaway catch-up burst can saturate the worker pool right when the
  system is already stressed.

If you want strict "exactly N fires per hour" semantics, do it in the
handler (e.g. record a counter).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pollypm.heartbeat.roster import (
    CronSchedule,
    EverySchedule,
    OnStartupSchedule,
    Roster,
    RosterEntry,
)


__all__ = ["Heartbeat", "JobQueueProtocol", "TickResult", "EnqueuedJob"]


class JobQueueProtocol(Protocol):
    """Structural interface for the queue dependency.

    Matches ``pollypm.jobs.JobQueue.enqueue`` so the tick is decoupled from
    the concrete queue implementation (great for tests).
    """

    def enqueue(
        self,
        handler_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class EnqueuedJob:
    """Record of one job enqueued during a tick — returned for observability."""

    handler_name: str
    payload: dict[str, Any]
    dedupe_key: str | None
    scheduled_for: datetime
    job_id: Any = None


@dataclass(slots=True)
class TickResult:
    """Result of a single ``tick()`` call."""

    tick_at: datetime
    enqueued: list[EnqueuedJob] = field(default_factory=list)
    skipped_not_due: int = 0
    last_tick_at: datetime | None = None

    @property
    def enqueued_count(self) -> int:
        return len(self.enqueued)


class Heartbeat:
    """Sealed heartbeat: ``tick(now)`` enqueues jobs for due roster entries.

    Construction binds the ``Roster`` + ``JobQueue`` dependencies — the
    heartbeat holds no other state beyond ``last_tick_at``.

    Usage::

        hb = Heartbeat(roster, queue)
        while True:
            result = hb.tick(datetime.now(timezone.utc))
            # observe result.enqueued_count, result.skipped_not_due
            sleep(60)
    """

    __slots__ = ("roster", "queue", "last_tick_at")

    def __init__(self, roster: Roster, queue: JobQueueProtocol) -> None:
        self.roster = roster
        self.queue = queue
        self.last_tick_at: datetime | None = None

    def tick(self, now: datetime) -> TickResult:
        if now.tzinfo is None:
            raise ValueError("tick() requires a timezone-aware datetime")

        prev = self.last_tick_at
        result = TickResult(tick_at=now, last_tick_at=prev)

        for entry in self.roster.entries:
            # Anchor first_seen_at on first observation so @every / @on_startup
            # have a deterministic reference.
            if entry.first_seen_at is None:
                entry.first_seen_at = now

            if self._is_due(entry, now, prev):
                payload_snapshot = copy.deepcopy(entry.payload)
                job_id = self.queue.enqueue(
                    entry.handler_name,
                    payload_snapshot,
                    dedupe_key=entry.dedupe_key,
                )
                entry.last_fired_at = now
                if isinstance(entry.schedule, OnStartupSchedule):
                    entry.on_startup_fired = True
                result.enqueued.append(
                    EnqueuedJob(
                        handler_name=entry.handler_name,
                        payload=payload_snapshot,
                        dedupe_key=entry.dedupe_key,
                        scheduled_for=now,
                        job_id=job_id,
                    )
                )
            else:
                result.skipped_not_due += 1

        self.last_tick_at = now
        return result

    # ------------------------------------------------------------------
    # Due-check dispatch
    # ------------------------------------------------------------------

    def _is_due(
        self,
        entry: RosterEntry,
        now: datetime,
        prev_tick: datetime | None,
    ) -> bool:
        sched = entry.schedule

        if isinstance(sched, OnStartupSchedule):
            return not entry.on_startup_fired

        if isinstance(sched, EverySchedule):
            return self._every_is_due(entry, sched, now)

        if isinstance(sched, CronSchedule):
            return self._cron_is_due(entry, sched, now, prev_tick)

        # Unknown schedule type — be strict, don't fire.
        return False

    def _every_is_due(
        self,
        entry: RosterEntry,
        sched: EverySchedule,
        now: datetime,
    ) -> bool:
        anchor = entry.first_seen_at or now
        if sched.interval.total_seconds() <= 0:
            return False
        if entry.last_fired_at is None:
            # First fire: due as soon as `now >= anchor + interval`? No —
            # @every means "every X", and the first fire after registration
            # at anchor+0 is at anchor+interval. But for @every 60s the user
            # typically expects the first tick to fire immediately OR at the
            # next boundary. We adopt "next boundary": first fire is at
            # anchor + interval (or any later tick, catch-up-once).
            return now >= anchor + sched.interval
        return now >= entry.last_fired_at + sched.interval

    def _cron_is_due(
        self,
        entry: RosterEntry,
        sched: CronSchedule,
        now: datetime,
        prev_tick: datetime | None,
    ) -> bool:
        # A cron entry is due if there exists a fire time strictly greater
        # than the later of (entry.last_fired_at, prev_tick, first_seen_at)
        # and ``<= now``. This gives us the catch-up-once guarantee.
        floor = entry.last_fired_at
        if floor is None:
            # Never fired — earliest window is the anchor (first time we saw it).
            # We use prev_tick if available for a tighter window; otherwise we
            # accept the first matching minute on or after first_seen_at.
            anchor = entry.first_seen_at or now
            # Look for any fire in (anchor - 1 minute, now]. Using anchor - 1m
            # so a cron that matches exactly at anchor fires on the first tick.
            from datetime import timedelta

            search_from = anchor - timedelta(minutes=1)
            next_fire = sched.next_due(search_from)
            return next_fire is not None and next_fire <= now
        next_fire = sched.next_due(floor)
        return next_fire is not None and next_fire <= now
