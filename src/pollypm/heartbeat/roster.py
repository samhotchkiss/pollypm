"""Roster entries and schedule parsing for the sealed heartbeat.

A ``Roster`` is a collection of ``RosterEntry`` objects. Each entry binds a
schedule expression to a job handler name plus a payload. The heartbeat tick
iterates the roster, asks each schedule "are you due?", and if so enqueues a
job on the shared ``JobQueue``.

Schedule expression grammar:

* ``@every <duration>`` — run every ``<duration>`` starting from the first
  tick after registration. ``<duration>`` accepts ``s``/``m``/``h`` suffixes
  (e.g. ``@every 60s``, ``@every 5m``, ``@every 1h``).
* ``@on_startup`` — run exactly once on the first tick after the entry is
  added to the roster.
* 5-field cron expressions (``minute hour day-of-month month day-of-week``)
  supporting ``*``, ``*/N``, comma-separated lists, ``A-B`` ranges.

Supported special strings:
    ``@hourly``  -> ``0 * * * *``
    ``@daily``   -> ``0 0 * * *``
    ``@weekly``  -> ``0 0 * * 0``
    ``@monthly`` -> ``0 0 1 * *``
    ``@yearly``, ``@annually`` -> ``0 0 1 1 *``

All schedule decisions use timezone-aware UTC datetimes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


__all__ = [
    "CronSchedule",
    "EverySchedule",
    "OnStartupSchedule",
    "Roster",
    "RosterEntry",
    "Schedule",
    "parse_schedule",
]


# ---------------------------------------------------------------------------
# Schedule types
# ---------------------------------------------------------------------------


class Schedule:
    """Abstract base: decides whether an entry is due at a given tick time."""

    def next_due(self, after: datetime) -> datetime | None:
        """Return the next datetime strictly greater than ``after`` when this
        schedule wants to fire, or ``None`` if it never will.

        Implementations must treat ``after`` as tz-aware UTC.
        """
        raise NotImplementedError

    def expression(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class OnStartupSchedule(Schedule):
    """Fires exactly once: on the first tick on or after ``registered_at``.

    The roster's ``first_seen_at`` acts as the "registered at" anchor so that
    an ``@on_startup`` entry fires on the first tick after it was added.
    """

    def next_due(self, after: datetime) -> datetime | None:  # noqa: D401
        # OnStartup is handled specially in Heartbeat.tick — we use the entry's
        # first_seen_at as the fire time. Returning None here would hide it.
        # We return a sentinel that the tick loop resolves via anchor.
        return None

    def expression(self) -> str:
        return "@on_startup"


@dataclass(frozen=True, slots=True)
class EverySchedule(Schedule):
    """Fires every ``interval`` from the entry's registration anchor."""

    interval: timedelta

    def next_due(self, after: datetime, *, anchor: datetime | None = None) -> datetime | None:
        if self.interval.total_seconds() <= 0:
            return None
        base = anchor if anchor is not None else after
        # Compute the first fire strictly greater than `after`.
        if after < base:
            return base
        delta = (after - base).total_seconds()
        n = int(delta // self.interval.total_seconds()) + 1
        return base + self.interval * n

    def expression(self) -> str:
        total = int(self.interval.total_seconds())
        if total % 3600 == 0:
            return f"@every {total // 3600}h"
        if total % 60 == 0:
            return f"@every {total // 60}m"
        return f"@every {total}s"


@dataclass(frozen=True, slots=True)
class CronSchedule(Schedule):
    """5-field cron schedule. Computes next fire minute by brute-force scan.

    ``minute`` / ``hour`` / ``dom`` / ``month`` / ``dow`` are frozensets of
    permitted integer values. Fields match when the current time component
    is in the set. Day-of-month and day-of-week use OR semantics when both
    are restricted (standard cron).
    """

    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    raw: str

    # Full ranges — used to detect restriction for dom/dow OR-semantics.
    _DOM_FULL = frozenset(range(1, 32))
    _DOW_FULL = frozenset(range(0, 7))

    def _matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        # cron day-of-month / day-of-week OR semantics: if either is restricted,
        # a match in either suffices; if both unrestricted, always match.
        dom_restricted = self.dom != self._DOM_FULL
        dow_restricted = self.dow != self._DOW_FULL
        # Python weekday: Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.dom
        dow_ok = cron_dow in self.dow
        if dom_restricted and dow_restricted:
            return dom_ok or dow_ok
        if dom_restricted:
            return dom_ok
        if dow_restricted:
            return dow_ok
        return True

    def next_due(self, after: datetime) -> datetime | None:
        # Start from the minute strictly after `after`.
        candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        # Bound the search: a cron expression either fires within ~4 years or never.
        limit = candidate + timedelta(days=366 * 4)
        while candidate <= limit:
            if self._matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None

    def expression(self) -> str:
        return self.raw


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


_CRON_ALIASES: dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_FIELD_RANGES: tuple[tuple[int, int], ...] = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day-of-month
    (1, 12),   # month
    (0, 6),    # day-of-week (0=Sun..6=Sat)
)


def parse_schedule(expr: str) -> Schedule:
    """Parse a schedule expression into a ``Schedule`` instance.

    Raises ``ValueError`` on unparseable input.
    """
    if not isinstance(expr, str):
        raise ValueError(f"schedule must be a string, got {type(expr).__name__}")
    text = expr.strip()
    if not text:
        raise ValueError("schedule expression is empty")

    if text == "@on_startup":
        return OnStartupSchedule()

    if text.startswith("@every"):
        remainder = text[len("@every") :].strip()
        return EverySchedule(interval=_parse_duration(remainder))

    if text in _CRON_ALIASES:
        text = _CRON_ALIASES[text]

    parts = text.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (minute hour dom month dow), got {len(parts)}: {expr!r}"
        )

    fields = tuple(
        _expand_field(part, lo, hi)
        for part, (lo, hi) in zip(parts, _FIELD_RANGES, strict=True)
    )
    return CronSchedule(
        minute=fields[0],
        hour=fields[1],
        dom=fields[2],
        month=fields[3],
        dow=fields[4],
        raw=text,
    )


def _parse_duration(text: str) -> timedelta:
    if not text:
        raise ValueError("@every requires a duration (e.g. '@every 60s')")
    # Accept plain seconds (int) or suffixed s/m/h/d.
    value = text.strip()
    unit = value[-1]
    if unit.isalpha():
        try:
            quantity = float(value[:-1])
        except ValueError as exc:
            raise ValueError(f"invalid duration: {text!r}") from exc
        if unit == "s":
            return timedelta(seconds=quantity)
        if unit == "m":
            return timedelta(minutes=quantity)
        if unit == "h":
            return timedelta(hours=quantity)
        if unit == "d":
            return timedelta(days=quantity)
        raise ValueError(f"unknown duration unit {unit!r}: {text!r}")
    # Bare number — treat as seconds.
    try:
        quantity = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid duration: {text!r}") from exc
    return timedelta(seconds=quantity)


def _expand_field(part: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for chunk in part.split(","):
        values.update(_expand_chunk(chunk, lo, hi))
    if not values:
        raise ValueError(f"empty cron field {part!r}")
    return frozenset(values)


def _expand_chunk(chunk: str, lo: int, hi: int) -> set[int]:
    step = 1
    body = chunk
    if "/" in chunk:
        body, step_str = chunk.split("/", 1)
        try:
            step = int(step_str)
        except ValueError as exc:
            raise ValueError(f"invalid step in cron chunk {chunk!r}") from exc
        if step <= 0:
            raise ValueError(f"cron step must be positive: {chunk!r}")
    if body == "*" or body == "":
        start, end = lo, hi
    elif "-" in body:
        start_s, end_s = body.split("-", 1)
        start, end = int(start_s), int(end_s)
    else:
        start = end = int(body)
    if start < lo or end > hi or start > end:
        raise ValueError(f"cron chunk {chunk!r} out of range [{lo},{hi}]")
    return set(range(start, end + 1, step))


# ---------------------------------------------------------------------------
# Roster entry + container
# ---------------------------------------------------------------------------


def _payload_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a payload dict — used for collision detection."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class RosterEntry:
    """Single registered recurring schedule.

    ``first_seen_at`` anchors ``@every`` and ``@on_startup`` semantics and is
    updated when the entry is added to the roster. ``last_fired_at`` tracks
    the most recent tick that enqueued a job — the tick uses it to enforce
    the "catch up at most once" overdue policy.
    """

    schedule: Schedule
    handler_name: str
    payload: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    first_seen_at: datetime | None = None
    last_fired_at: datetime | None = None
    on_startup_fired: bool = False

    def identity(self) -> str:
        """Stable identity for collision detection: handler + payload hash."""
        return f"{self.handler_name}:{_payload_hash(self.payload)}"


@dataclass(slots=True)
class Roster:
    """Mutable collection of ``RosterEntry`` objects.

    Collisions (same ``identity()``) are reported by ``register`` — the caller
    chooses whether to log, warn, or replace. ``register_entry`` returns
    ``True`` when the entry is new, ``False`` when it was a duplicate.
    """

    entries: list[RosterEntry] = field(default_factory=list)

    def register_entry(self, entry: RosterEntry) -> bool:
        ident = entry.identity()
        for existing in self.entries:
            if existing.identity() == ident:
                return False
        self.entries.append(entry)
        return True

    def register(
        self,
        *,
        schedule: str | Schedule,
        handler_name: str,
        payload: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        now: datetime | None = None,
    ) -> tuple[RosterEntry, bool]:
        """Register a recurring schedule. Returns (entry, is_new)."""
        sched = schedule if isinstance(schedule, Schedule) else parse_schedule(schedule)
        entry = RosterEntry(
            schedule=sched,
            handler_name=handler_name,
            payload=dict(payload or {}),
            dedupe_key=dedupe_key,
            first_seen_at=now,
        )
        inserted = self.register_entry(entry)
        if not inserted:
            # Return the existing entry with matching identity so the caller
            # can inspect it if useful.
            ident = entry.identity()
            for existing in self.entries:
                if existing.identity() == ident:
                    return existing, False
        return entry, True

    def snapshot(self) -> list[RosterEntry]:
        """Return a shallow copy of registered entries (for introspection)."""
        return list(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)
