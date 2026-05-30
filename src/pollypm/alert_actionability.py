"""Shared user-actionable alert filtering for cockpit surfaces.

This module is intentionally storage-light: callers hand it alert rows and,
when available, the small workspace context needed to demote stale watchdog
alerts. The web alert list, dashboard rollups, and Home headline should all
consult this policy instead of each keeping local alert-count rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from pollypm.cockpit_alerts import is_operational_alert


_STUCK_ON_TASK_PREFIX = "stuck_on_task:"
_QUEUE_WITHOUT_MOTION_SESSION_PREFIX = "audit-queue_without_motion-"
_WATCHDOG_ALERT_TYPE = "audit_watchdog"
_QWM_PROJECT_RE = re.compile(r"\bProject\s+(?P<project>[A-Za-z0-9_.-]+)\s+has\b")


@dataclass(slots=True, frozen=True)
class AlertActionabilityContext:
    """Workspace facts used by :func:`is_user_actionable_alert`."""

    user_waiting_task_ids: frozenset[str] = frozenset()
    known_projects: frozenset[str] | None = None
    tracked_projects: frozenset[str] | None = None
    project_task_counts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)


def _row_value(row: object, *names: str) -> object:
    if isinstance(row, Mapping):
        for name in names:
            value = row.get(name)
            if value is not None:
                return value
        return None
    for name in names:
        value = getattr(row, name, None)
        if value is not None:
            return value
    return None


def _text_value(row: object, *names: str) -> str:
    value = _row_value(row, *names)
    return str(value or "")


def _stuck_alert_already_user_waiting(
    alert_type: str,
    user_waiting_task_ids: frozenset[str],
) -> bool:
    if not alert_type.startswith(_STUCK_ON_TASK_PREFIX):
        return False
    task_id = alert_type[len(_STUCK_ON_TASK_PREFIX):].strip()
    return bool(task_id and task_id in user_waiting_task_ids)


def _queue_without_motion_project(
    alert: object,
    *,
    known_projects: frozenset[str] | None,
) -> str | None:
    session_name = _text_value(alert, "session_name", "scope")
    if not session_name.startswith(_QUEUE_WITHOUT_MOTION_SESSION_PREFIX):
        return None

    message = _text_value(alert, "message", "body")
    match = _QWM_PROJECT_RE.search(message)
    if match is not None:
        return match.group("project")

    tail = session_name[len(_QUEUE_WITHOUT_MOTION_SESSION_PREFIX):]
    if known_projects:
        for project in sorted(known_projects, key=len, reverse=True):
            if tail == project or tail.startswith(project + "-"):
                return project
    return None


def _queue_without_motion_is_stale(
    alert: object,
    *,
    context: AlertActionabilityContext,
) -> bool:
    alert_type = _text_value(alert, "alert_type", "type")
    if alert_type != _WATCHDOG_ALERT_TYPE:
        return False

    project = _queue_without_motion_project(
        alert,
        known_projects=context.known_projects or context.tracked_projects,
    )
    if not project:
        return False

    if context.tracked_projects is not None and project not in context.tracked_projects:
        return True

    counts = context.project_task_counts.get(project)
    if counts is not None and int(counts.get("queued", 0) or 0) <= 0:
        return True

    return False


def is_user_actionable_alert(
    alert: object,
    *,
    context: AlertActionabilityContext | None = None,
    include_operational: bool = False,
) -> bool:
    """Return whether ``alert`` belongs on user-facing action surfaces."""

    alert_type = _text_value(alert, "alert_type", "type")
    if not include_operational and is_operational_alert(alert_type):
        return False

    ctx = context or AlertActionabilityContext()
    if _stuck_alert_already_user_waiting(alert_type, ctx.user_waiting_task_ids):
        return False
    if _queue_without_motion_is_stale(alert, context=ctx):
        return False

    return True


def user_actionable_alerts(
    alerts: Iterable[object],
    *,
    context: AlertActionabilityContext | None = None,
    include_operational: bool = False,
) -> list[object]:
    """Filter alert rows to the set that should drive Home/Alerts counts."""

    return [
        alert
        for alert in alerts
        if is_user_actionable_alert(
            alert,
            context=context,
            include_operational=include_operational,
        )
    ]


def count_user_actionable_alerts(
    alerts: Iterable[object],
    *,
    context: AlertActionabilityContext | None = None,
    include_operational: bool = False,
) -> int:
    """Count the same filtered set returned by :func:`user_actionable_alerts`."""

    return len(
        user_actionable_alerts(
            alerts,
            context=context,
            include_operational=include_operational,
        )
    )


__all__ = [
    "AlertActionabilityContext",
    "count_user_actionable_alerts",
    "is_user_actionable_alert",
    "user_actionable_alerts",
]
