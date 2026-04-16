"""Sealed heartbeat tick module.

The heartbeat's entire responsibility is to consult a roster of recurring
schedules and enqueue corresponding jobs. No domain logic lives here —
handlers run elsewhere (see ``pollypm.jobs``).

Public API:

    >>> from pollypm.heartbeat import Heartbeat, Roster, RosterEntry, TickResult

The tick is a pure function of ``(now, roster, queue)`` plus minimal
``last_tick_at`` bookkeeping used to document overdue behavior.
"""

from __future__ import annotations

from pollypm.heartbeat.roster import (
    Roster,
    RosterEntry,
    Schedule,
    parse_schedule,
)
from pollypm.heartbeat.tick import Heartbeat, JobQueueProtocol, TickResult

__all__ = [
    "Heartbeat",
    "JobQueueProtocol",
    "Roster",
    "RosterEntry",
    "Schedule",
    "TickResult",
    "parse_schedule",
]
