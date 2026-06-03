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
from pollypm.project_liveness import project_has_real_stalled_work


_STUCK_ON_TASK_PREFIX = "stuck_on_task:"
_QUEUE_WITHOUT_MOTION_SESSION_PREFIX = "audit-queue_without_motion-"
_WORKTREE_STATE_PREFIX = "worktree_state:"
_WATCHDOG_ALERT_TYPE = "audit_watchdog"
_QWM_PROJECT_RE = re.compile(r"\bProject\s+(?P<project>[A-Za-z0-9_.-]+)\s+has\b")
_TASK_PAYLOAD_RE = re.compile(r"^(?P<project>[A-Za-z0-9_.-]+)/\d+(?:\b|:)")

# #2475/#2480 — system-internal recovery watchdog warns. Each of these
# alert types is a mechanical recovery signal raised by the
# task-assignment sweep that the heartbeat/cascade auto-handles
# (re-spawn the per-task worker, refresh the plan gate, claim the
# queued task). They are NOT operator decisions. The operator-Home "N
# things need you" headline was dominated by these warns firing on
# stale / archived / synthetic test projects (``pm_test_*``,
# ``ghost``, ``alpha``, ``queuestorm_*``, ...) — many of which were
# still tracked, so trackedness alone was a false liveness signal. We
# demote a recovery warn when it is scoped to a project that is not
# tracked, synthetic, or has neither recent real completed work nor real
# stalled work. The session-name → project extraction mirrors the
# synthetic scope strings the sweep writes (see
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
    recent_real_work_projects: frozenset[str] | None = None
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


def _known_project_match(
    value: str,
    *,
    known_projects: frozenset[str] | None,
) -> str | None:
    if not value:
        return None
    if not known_projects:
        return None
    for project in sorted(known_projects, key=len, reverse=True):
        if (
            value == project
            or value.startswith(project + "/")
            or value.startswith(project + ":")
        ):
            return project
    return None


def _task_payload_project(value: str) -> str | None:
    match = _TASK_PAYLOAD_RE.match(value.strip())
    if match is None:
        return None
    return match.group("project") or None


def _alert_type_payload_project(
    alert_type: str,
    *,
    known_projects: frozenset[str] | None,
) -> str | None:
    if ":" not in alert_type:
        return None
    _family, _sep, payload = alert_type.partition(":")
    payload = payload.strip()
    if not payload:
        return None
    project = _task_payload_project(payload)
    if project is None:
        project = _known_project_match(payload, known_projects=known_projects)
    if project is None:
        return None
    if known_projects is not None and project not in known_projects:
        return None
    return project


def _session_prefix_project(
    session_name: str,
    *,
    prefixes: tuple[str, ...],
    known_projects: frozenset[str] | None,
    allow_numeric_tail: bool,
) -> str | None:
    for prefix in prefixes:
        candidate_prefixes = [prefix]
        if prefix.endswith("-"):
            candidate_prefixes.append(prefix[:-1] + "_")
        for candidate_prefix in candidate_prefixes:
            if not session_name.startswith(candidate_prefix):
                continue
            tail = session_name[len(candidate_prefix):].strip()
            project = _known_project_match(tail, known_projects=known_projects)
            if project is not None:
                return project
            if allow_numeric_tail:
                for separator in ("-", "_"):
                    if separator not in tail:
                        continue
                    head, _sep, number = tail.rpartition(separator)
                    if head and number.isdigit():
                        return head
    return None


def alert_project_key(
    alert: object,
    *,
    known_projects: frozenset[str] | None = None,
) -> str | None:
    """Return the project key encoded by a project-scoped alert, if known."""

    alert_type = _text_value(alert, "alert_type", "type")
    session_name = _text_value(alert, "session_name", "scope")

    project = _queue_without_motion_project(
        alert,
        known_projects=known_projects,
    )
    if project:
        return project

    project = _recovery_watchdog_project(
        alert,
        alert_type,
        known_projects=known_projects,
    )
    if project:
        return project

    project = _worktree_state_project(
        alert_type,
        known_projects=known_projects,
    )
    if project:
        return project

    project = _alert_type_payload_project(
        alert_type,
        known_projects=known_projects,
    )
    if project:
        return project

    project = _session_prefix_project(
        session_name,
        prefixes=("review-", "blocked-", "task-"),
        known_projects=known_projects,
        allow_numeric_tail=True,
    )
    if project:
        return project

    return _session_prefix_project(
        session_name,
        prefixes=("worker-", "architect-", "reviewer-", "pm-"),
        known_projects=known_projects,
        allow_numeric_tail=False,
    )


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
    message = _text_value(alert, "message", "body")
    alert_type = _text_value(alert, "alert_type", "type")
    if alert_type == _WATCHDOG_ALERT_TYPE:
        match = _QWM_PROJECT_RE.search(message)
        if match is not None:
            return match.group("project")

    if not session_name.startswith(_QUEUE_WITHOUT_MOTION_SESSION_PREFIX):
        return None

    match = _QWM_PROJECT_RE.search(message)
    if match is not None:
        return match.group("project")

    tail = session_name[len(_QUEUE_WITHOUT_MOTION_SESSION_PREFIX):]
    if known_projects:
        for project in sorted(known_projects, key=len, reverse=True):
            if tail == project:
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

    if _project_is_inactive_for_operator_count(project, context=context):
        return True

    counts = context.project_task_counts.get(project)
    if counts is not None and int(counts.get("queued", 0) or 0) <= 0:
        return True

    return False


def _worktree_state_project(
    alert_type: str,
    *,
    known_projects: frozenset[str] | None,
) -> str | None:
    if not alert_type.startswith(_WORKTREE_STATE_PREFIX):
        return None
    tail = alert_type[len(_WORKTREE_STATE_PREFIX):].strip()
    if not tail:
        return None
    # Production shape is ``worktree_state:<project>/<task>:<reason>``.
    if "/" in tail:
        return tail.split("/", 1)[0].strip() or None
    if known_projects:
        for project in sorted(known_projects, key=len, reverse=True):
            if tail == project or tail.startswith(project + ":"):
                return project
    return tail.split(":", 1)[0].strip() or None


def _project_is_inactive_for_operator_count(
    project: str,
    *,
    context: AlertActionabilityContext,
) -> bool:
    if context.tracked_projects is not None and project not in context.tracked_projects:
        return True
    if (
        context.recent_real_work_projects is not None
        and project not in context.recent_real_work_projects
    ):
        return True
    return False


def _worktree_state_is_stale(
    alert_type: str,
    *,
    context: AlertActionabilityContext,
) -> bool:
    # Orphan worktree cleanup is mechanical GC; the audit handler
    # self-heals terminal rows and leaves non-terminal rows as FYI logs.
    if alert_type.endswith(":orphan_branch"):
        return True
    project = _worktree_state_project(
        alert_type,
        known_projects=context.known_projects or context.tracked_projects,
    )
    if not project:
        return False
    return _project_is_inactive_for_operator_count(project, context=context)


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
    so we strip the ``/<task-number>`` tail. When a ``known_projects``
    set is supplied, require an exact match so a ghost project such as
    ``media-old`` cannot be counted against ``media``.
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
            if tail == project:
                return project
    return tail


def _recovery_watchdog_warn_is_self_healing(
    alert: object,
    alert_type: str,
    *,
    context: AlertActionabilityContext,
) -> bool:
    """Return True for a recovery warn the cascade auto-handles itself.

    A recovery warn on a stale / archived / synthetic project is
    system-internal noise the heartbeat sweep is already retrying.
    Counting it in the operator's "N things need you" headline trains
    the operator to ignore the number (#2475/#2480). Trackedness is not
    enough: watchdog churn can keep a dead tracked project looking
    freshly touched, so callers may also pass recent real ``done`` work
    and task-count facts. A real tracked project with queued / blocked /
    in-progress work is still operator-relevant before its first done
    task (#2545).
    """

    if alert_type not in _RECOVERY_WATCHDOG_SESSION_PREFIXES:
        return False
    if context.tracked_projects is None and context.recent_real_work_projects is None:
        # Without any liveness facts we cannot tell stale from live, so
        # keep the warn actionable rather than over-suppress.
        return False
    project = _recovery_watchdog_project(
        alert,
        alert_type,
        known_projects=context.known_projects or context.tracked_projects,
    )
    if not project:
        return False
    if project_has_real_stalled_work(
        project,
        context.project_task_counts.get(project),
    ):
        return False
    return _project_is_inactive_for_operator_count(project, context=context)


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
    if _worktree_state_is_stale(alert_type, context=ctx):
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
    "alert_project_key",
    "count_user_actionable_alerts",
    "is_user_actionable_alert",
    "user_actionable_alerts",
]
