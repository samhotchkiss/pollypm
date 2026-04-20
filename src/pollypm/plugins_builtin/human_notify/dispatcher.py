"""Dispatch a ``TaskAssignmentEvent`` across every enabled adapter.

The dispatcher is the single point that filters for
``ActorType.HUMAN`` and fans out to adapters. Keeping the filter
here (not in the bus subscriber) means third-party adapters that
want to observe non-HUMAN events can still subscribe directly to
the bus without going through us.

Design choices:

- **Every adapter runs, even if an earlier one succeeded.** A user
  who configures both macOS + webhook almost certainly wants
  both — they're at their laptop AND they want their phone to
  buzz. The cockpit fallback likewise always fires so the alert
  is recorded even when the OS banner is dismissed before it's
  seen.
- **One adapter's failure never blocks another.** Each adapter
  is wrapped in its own try/except; the dispatcher logs and moves
  on. This is the same invariant :mod:`task_assignment_notify`
  applies to its session dispatch.
- **No retries.** Adapters that want retry semantics (e.g. the
  webhook adapter backing off on 5xx) handle it internally — the
  dispatcher treats each call as fire-and-forget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from pollypm.work.task_assignment import TaskAssignmentEvent

    from .protocol import HumanNotifyAdapter

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DispatchResult:
    """Outcome summary for a single dispatch call.

    ``delivered`` counts adapters that returned without raising;
    ``skipped`` counts adapters whose ``is_available`` returned
    False. The two should sum to the total number of registered
    adapters.
    """
    delivered: int
    skipped: int


def format_title(event: "TaskAssignmentEvent") -> str:
    """Short, banner-friendly subject line."""
    verb = "Review" if event.work_status == "review" else "Action needed"
    return f"PollyPM: {verb} — {event.project}"


def format_body(event: "TaskAssignmentEvent") -> str:
    """Two-line body: task title + inbox-pointer for easy follow-up.

    Keeping the body terse means macOS banners stay readable and
    webhook services (ntfy, Slack) don't wrap awkwardly. Users
    who want more detail open the cockpit Inbox or run
    ``pm inbox show <task_id>``.
    """
    title = event.task_title or event.task_id
    return f"{title}\npm inbox show {event.task_id}"


def dispatch(
    event: "TaskAssignmentEvent",
    adapters: Iterable["HumanNotifyAdapter"],
) -> DispatchResult:
    """Fan out ``event`` to every available adapter. Returns a summary.

    No-ops for non-HUMAN events — callers are expected to filter
    before calling us, but the guard here is defensive so a
    misconfigured subscriber can't accidentally spam users with
    worker-addressed pings.
    """
    # Local import to dodge a cycle at plugin-host load time.
    from pollypm.work.models import ActorType

    actor_type = getattr(event, "actor_type", None)
    if actor_type is not ActorType.HUMAN:
        return DispatchResult(delivered=0, skipped=0)

    title = format_title(event)
    body = format_body(event)

    delivered = 0
    skipped = 0
    for adapter in adapters:
        try:
            available = adapter.is_available()
        except Exception:  # noqa: BLE001
            logger.debug(
                "human_notify: adapter %r is_available() raised",
                getattr(adapter, "name", "?"), exc_info=True,
            )
            available = False
        if not available:
            skipped += 1
            continue
        try:
            adapter.notify(
                title=title,
                body=body,
                task_id=event.task_id,
                project=event.project,
            )
            delivered += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "human_notify: adapter %r notify() failed for %s",
                getattr(adapter, "name", "?"), event.task_id,
                exc_info=True,
            )
    return DispatchResult(delivered=delivered, skipped=skipped)


__all__ = ["DispatchResult", "dispatch", "format_title", "format_body"]
