"""Task-assignment alert cleanup events.

Work-service transitions publish cleanup intent here when an assignment
alert becomes stale. The task_assignment_notify plugin subscribes when it
is loaded; without a subscriber these events are a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class CancelledTaskAssignmentAlertsEvent:
    """A cancelled task's assignment alerts can be cleared."""

    task_id: str
    project: str
    role_names: tuple[str, ...]
    has_other_active_for_role: Mapping[str, bool]
    store: object | None = None


@dataclass(slots=True, frozen=True)
class ClearNoSessionAlertForTaskEvent:
    """A task-level no-session assignment alert can be cleared."""

    task_id: str
    store: object | None = None


TaskAssignmentAlertEvent: TypeAlias = (
    CancelledTaskAssignmentAlertsEvent | ClearNoSessionAlertForTaskEvent
)
TaskAssignmentAlertListener = Callable[[TaskAssignmentAlertEvent], None]

_listeners: list[TaskAssignmentAlertListener] = []


def register_listener(listener: TaskAssignmentAlertListener) -> None:
    """Register ``listener`` for assignment-alert cleanup events."""
    if listener not in _listeners:
        _listeners.append(listener)


def unregister_listener(listener: TaskAssignmentAlertListener) -> None:
    """Remove a previously-registered cleanup listener."""
    try:
        _listeners.remove(listener)
    except ValueError:
        pass


def clear_listeners() -> None:
    """Drop every registered listener. Intended for tests."""
    _listeners.clear()


def dispatch(event: TaskAssignmentAlertEvent) -> None:
    """Deliver ``event`` to registered subscribers.

    Listener errors are logged and swallowed so alert hygiene can never
    block the work transition that made the alert stale.
    """
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001
            logger.exception(
                "task_assignment_alert listener %r raised on %s",
                getattr(listener, "__name__", listener),
                getattr(event, "task_id", "?"),
            )
