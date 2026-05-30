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

# #2475 — system-internal recovery watchdog warns. Each of these alert
# types is a mechanical recovery signal raised by the task-assignment
# sweep that the heartbeat/cascade auto-handles (re-spawn the per-task
# worker, refresh the plan gate, claim the queued task). They are NOT
# operator decisions. On a live workspace the operator-Home "N things
# need you" headline was dominated by these warns firing on stale /
# archived / synthetic test projects (``pm_test_*``, ``ghost``,
# ``alpha``, ``queuestorm_*``, ...) — none of which the operator can or
# should act on. We demote a recovery warn from the operator-action
# count when it is scoped to a project that is NOT tracked. A warn on a
# tracked project still surfaces (the operator may legitimately want to
# claim its queued work), so genuine operator-decision signals are
# preserved. The session-name → project extraction mirrors the synthetic
# scope strings the sweep writes (see
# ``plugins_builtin/task_assignment_notify/handlers/sweep.py``):
#   plan_missing        -> ``plan_gate-<project>``
#   worker_session_gap  -> ``worker_session_gap-<project>``
#   missing_task_worker -> ``missing_task_worker-<project>/<task-number>``
_RECOVERY_WATCHDOG_SESSION_PREFIXES: dict[str, str] = {
    "plan_missing": "plan_gate-",
    "worker_session_gap": "worker_session_gap-",
    "missing_task_worker": "missing_task_worker-",
}


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


def _recovery_watchdog_project(
    alert: object,
    alert_type: str,
    *,
    known_projects: frozenset[str] | None,
) -> str | None:
    """Return the project a recovery-watchdog warn is scoped to, or ``None``.

    The four recovery warns encode their project in a synthetic session
    name. ``plan_missing`` / ``worker_session_gap`` carry the project key
    directly (``plan_gate-<project>`` / ``worker_session_gap-<project>``).
    ``missing_task_worker`` is keyed per task (``missing_task_worker-<project>/<N>``)
    so we strip the ``/<task-number>`` tail. Project keys can themselves
    contain hyphens, so when a ``known_projects`` set is supplied we
    prefer the longest matching key to disambiguate.
    """

    prefix = _RECOVERY_WATCHDOG_SESSION_PREFIXES.get(alert_type)
    if prefix is None:
        return None
    session_name = _text_value(alert, "session_name", "scope")
    if not session_name.startswith(prefix):
        return None
    tail = session_name[len(prefix):]
    if alert_type == "missing_task_worker":
        # ``<project>/<task-number>`` — the project is everything before
        # the last slash (project keys never contain a slash).
        tail = tail.rsplit("/", 1)[0]
    tail = tail.strip()
    if not tail:
        return None
    if known_projects:
        for project in sorted(known_projects, key=len, reverse=True):
            if tail == project or tail.startswith(project + "-"):
                return project
    return tail


def _recovery_watchdog_warn_is_self_healing(
    alert: object,
    alert_type: str,
    *,
    context: AlertActionabilityContext,
) -> bool:
    """Return True for a recovery warn the cascade auto-handles itself.

    Today the demotion is scoped to *non-tracked* projects: a recovery
    warn on a tracked project is left actionable (the operator may want
    to claim its queued work), but a warn on a stale / archived /
    synthetic test project is system-internal noise the heartbeat sweep
    is already retrying — counting it in the operator's "N things need
    you" headline trains the operator to ignore the number (#2475).
    """

    if alert_type not in _RECOVERY_WATCHDOG_SESSION_PREFIXES:
        return False
    if context.tracked_projects is None:
        # Without a tracked-project set we cannot tell stale from live,
        # so we keep the warn actionable rather than over-suppress.
        return False
    project = _recovery_watchdog_project(
        alert,
        alert_type,
        known_projects=context.known_projects or context.tracked_projects,
    )
    if not project:
        return False
    return project not in context.tracked_projects


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
    if _recovery_watchdog_warn_is_self_healing(alert, alert_type, context=ctx):
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
