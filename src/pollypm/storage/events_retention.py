"""Tiered retention policy for the ``events`` table.

The ``events`` table is written to constantly — every heartbeat sweep, every
token-ledger bump, every task transition, every inbox message. Left
unbounded it becomes the single largest contributor to ``state.db`` bloat
(45k rows and growing on Sam's live DB as of 2026-04-17). The blanket
7-day prune in :func:`StateStore.prune_old_data` used to keep it honest, but
that policy is both too aggressive (drops task approvals after a week —
useful audit trail gone) AND too lax (lets heartbeat_error rows accumulate
for a full week when 24h is plenty).

This module defines a **tiered retention policy**. Each ``event_type`` is
classified into one of four tiers, each with its own retention window:

* ``audit`` — 365 days. Task transitions, approvals, rejections, launches,
  recoveries, alerts. Anything that forms a long-term audit trail.
* ``operational`` — 30 days. Lease churn, send_input, nudge, delivery —
  useful for post-mortem on recent incidents but not worth keeping a year.
* ``high_volume`` — 7 days. Heartbeats, token-ledger entries, scheduler
  tick rows. Pure noise after a week.
* ``default`` — 30 days. Any event_type not explicitly mapped falls
  through to this tier so brand-new event_types don't accumulate
  unbounded while someone figures out where they belong.

The policy is **data** — tier membership lives in the constants below and
can be tuned in one place. The sweep itself is a single parameterized
``DELETE`` per tier using an ``IN (?, ?, ?...)`` clause so SQLite plans it
as an index-friendly scan rather than row-by-row Python.

See issue #267 context and the ``events.retention_sweep`` handler in
``core_recurring/plugin.py`` for the cron wiring.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable


# ---------------------------------------------------------------------------
# Tier membership
# ---------------------------------------------------------------------------
#
# Maps are frozensets for cheap membership checks. Order between tiers is
# ``audit > operational > high_volume`` — if a type ever appears in two
# tiers (it shouldn't) the sweep uses whichever tier iterates first; we
# defensively keep them disjoint.


AUDIT_EVENT_TYPES: frozenset[str] = frozenset({
    "task.approved",
    "task.rejected",
    "task.done",
    "task.claimed",
    "task.queued",
    "plan.approved",
    "inbox.message.created",
    "launch",
    "recovered",
    "recovery_prompt",
    "state_drift",
    "persona_swap_detected",
    "alert",
    "escalated",
})


OPERATIONAL_EVENT_TYPES: frozenset[str] = frozenset({
    "lease",
    "stop",
    "send_input",
    "nudge",
    "ran",
    "processed",
    "stabilize_failed",
    "delivery",
})


HIGH_VOLUME_EVENT_TYPES: frozenset[str] = frozenset({
    "heartbeat",
    "heartbeat_error",
    "token_ledger",
    "scheduled",
})


# Sanity check at import time — if a type ever slips into two tiers we
# want the ImportError up front, not a silent overlap that deletes rows
# twice (or, worse, under the wrong window).
_OVERLAP = (
    (AUDIT_EVENT_TYPES & OPERATIONAL_EVENT_TYPES)
    | (AUDIT_EVENT_TYPES & HIGH_VOLUME_EVENT_TYPES)
    | (OPERATIONAL_EVENT_TYPES & HIGH_VOLUME_EVENT_TYPES)
)
if _OVERLAP:  # pragma: no cover — structural assertion
    raise RuntimeError(
        f"events_retention: event types classified into multiple tiers: {_OVERLAP!r}"
    )


# ---------------------------------------------------------------------------
# Retention policy + sweep
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class RetentionPolicy:
    """Retention windows in days for each tier.

    Defaults mirror the ``EventsRetentionSettings`` dataclass on
    ``PollyPMConfig`` so handlers that don't wire the config still get
    sensible behaviour.
    """

    audit_days: int = 365
    operational_days: int = 30
    high_volume_days: int = 7
    default_days: int = 30


@dataclass(slots=True, frozen=True)
class RetentionSweepResult:
    """Counts returned by :func:`sweep_events`."""

    deleted_audit: int = 0
    deleted_operational: int = 0
    deleted_high_volume: int = 0
    deleted_default: int = 0

    @property
    def total(self) -> int:
        return (
            self.deleted_audit
            + self.deleted_operational
            + self.deleted_high_volume
            + self.deleted_default
        )


def _delete_by_types(
    conn: sqlite3.Connection,
    event_types: Iterable[str],
    cutoff_iso: str,
) -> int:
    """DELETE rows whose type is in ``event_types`` AND older than cutoff.

    Uses a single parameterized IN clause rather than row-by-row deletes.
    Returns the row count.
    """
    types = tuple(event_types)
    if not types:
        return 0
    placeholders = ",".join("?" for _ in types)
    sql = (
        f"DELETE FROM events "
        f"WHERE event_type IN ({placeholders}) AND created_at < ?"
    )
    cursor = conn.execute(sql, (*types, cutoff_iso))
    return int(cursor.rowcount or 0)


def _delete_default_tier(
    conn: sqlite3.Connection,
    known_types: Iterable[str],
    cutoff_iso: str,
) -> int:
    """DELETE rows whose type is NOT in any known tier, older than cutoff.

    The default tier is everything we didn't explicitly classify — we
    express it via ``NOT IN (<known>)`` so new event_types land here by
    accident rather than living forever.
    """
    types = tuple(known_types)
    if not types:
        # Degenerate: no classified types means everything falls to default.
        cursor = conn.execute(
            "DELETE FROM events WHERE created_at < ?", (cutoff_iso,),
        )
        return int(cursor.rowcount or 0)
    placeholders = ",".join("?" for _ in types)
    sql = (
        f"DELETE FROM events "
        f"WHERE event_type NOT IN ({placeholders}) AND created_at < ?"
    )
    cursor = conn.execute(sql, (*types, cutoff_iso))
    return int(cursor.rowcount or 0)


def sweep_events(
    conn: sqlite3.Connection,
    policy: RetentionPolicy | None = None,
    *,
    now: datetime | None = None,
) -> RetentionSweepResult:
    """Apply the tiered retention policy against the ``events`` table.

    Runs four DELETEs, one per tier, committing once at the end. Safe
    to call concurrently with readers — SQLite serialises writes on the
    shared connection and the shared ``busy_timeout`` coordinates with
    any other writers.

    ``conn`` is the caller's SQLite connection (usually
    ``StateStore._conn``). The caller owns the connection lifecycle and
    any thread-lock it may have wrapped around it; this function just
    issues the four DELETEs + a single commit.

    Returns a :class:`RetentionSweepResult` — zero counts are fine.
    """
    if policy is None:
        policy = RetentionPolicy()
    if now is None:
        now = datetime.now(UTC)

    audit_cutoff = (now - timedelta(days=policy.audit_days)).isoformat()
    operational_cutoff = (
        now - timedelta(days=policy.operational_days)
    ).isoformat()
    high_volume_cutoff = (
        now - timedelta(days=policy.high_volume_days)
    ).isoformat()
    default_cutoff = (now - timedelta(days=policy.default_days)).isoformat()

    known_types: set[str] = set()
    known_types.update(AUDIT_EVENT_TYPES)
    known_types.update(OPERATIONAL_EVENT_TYPES)
    known_types.update(HIGH_VOLUME_EVENT_TYPES)

    deleted_audit = _delete_by_types(conn, AUDIT_EVENT_TYPES, audit_cutoff)
    deleted_operational = _delete_by_types(
        conn, OPERATIONAL_EVENT_TYPES, operational_cutoff,
    )
    deleted_high_volume = _delete_by_types(
        conn, HIGH_VOLUME_EVENT_TYPES, high_volume_cutoff,
    )
    deleted_default = _delete_default_tier(conn, known_types, default_cutoff)

    conn.commit()

    return RetentionSweepResult(
        deleted_audit=deleted_audit,
        deleted_operational=deleted_operational,
        deleted_high_volume=deleted_high_volume,
        deleted_default=deleted_default,
    )
