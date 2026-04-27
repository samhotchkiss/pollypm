"""Project blocker and monitoring summaries.

These helpers keep the product surfaces separate:
- activity/dashboard summaries are durable Store events;
- user action is materialized as a work-service task assigned to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable


@dataclass(slots=True)
class ProjectBlockerSummary:
    project: str
    reason: str
    owner: str
    required_actions: list[str] = field(default_factory=list)
    affected_tasks: list[str] = field(default_factory=list)
    unblock_condition: str = ""


@dataclass(slots=True)
class ProjectMonitorSummary:
    project: str
    completed_since_last: list[str] = field(default_factory=list)
    stalled_tasks: list[str] = field(default_factory=list)
    human_blockers: list[str] = field(default_factory=list)
    automatic_next_actions: list[str] = field(default_factory=list)
    next_check_at: str | None = None
    #: ``True`` when no tasks completed in the lookback window — the
    #: zero-completion callout the issue asks for. Recorded explicitly
    #: so downstream renderers don't have to re-derive it from the
    #: empty ``completed_since_last`` list (an empty list could also
    #: mean "we haven't checked yet" without this flag).
    zero_completion_window: bool = False
    #: Lookback window the summary covers — ISO-8601 string.
    window_started_at: str | None = None


# Hard-coded for now: how far back to look when computing
# ``completed_since_last`` if the caller doesn't supply a ``since``.
# Two hours matches the cockpit's default "while you were away"
# horizon and the advisor's stagnation cadence.
_DEFAULT_MONITOR_WINDOW = timedelta(hours=2)


def _as_aware_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _task_completed_in_window(task: Any, window_started: datetime) -> bool:
    for transition in getattr(task, "transitions", []) or []:
        to_state = getattr(transition, "to_state", None)
        if to_state is None and isinstance(transition, dict):
            to_state = transition.get("to_state")
        if to_state != "done":
            continue
        timestamp = getattr(transition, "timestamp", None)
        if timestamp is None and isinstance(transition, dict):
            timestamp = transition.get("timestamp") or transition.get("created_at")
        completed_at = _as_aware_utc(timestamp)
        if completed_at is not None and completed_at >= window_started:
            return True
    return False


def compute_project_monitor_summary(
    *,
    work_service: Any,
    project_key: str,
    since: datetime | None = None,
    next_check_at: str | None = None,
) -> ProjectMonitorSummary:
    """Walk a project's work service and produce a complete summary (#782).

    Fills every field on :class:`ProjectMonitorSummary` so durable
    monitor records carry the full picture the issue asks for:

    * ``completed_since_last`` — task IDs that hit ``done`` since the
      lookback window opened. Drives the cockpit's "what got done
      while you were away" copy and the activity feed.
    * ``stalled_tasks`` — IDs that are ``in_progress`` but haven't
      moved within the window. Distinct from ``human_blockers`` (no
      user action required) and ``completed_since_last`` (active but
      not done).
    * ``human_blockers`` — IDs in ``waiting_on_user`` / ``on_hold`` /
      ``blocked`` states. The dashboard / monitor renderer flags
      these as needing user action so they're separable from work
      Polly can advance automatically.
    * ``automatic_next_actions`` — plain-language descriptions of
      what Polly will try next without user input (claim a queued
      task, retry a stalled worker). Mostly seeded by callers; the
      helper just leaves the slot open.
    * ``zero_completion_window`` — explicit flag for the "no tasks
      completed in the window" case. The issue calls out this signal
      specifically because monitor output frequently buries it.
    * ``window_started_at`` — the lookback boundary so renderers can
      say "no completions in the last 2h" without re-deriving the
      window.

    The walk is best-effort: if a ``work_service`` query raises (DB
    closed, malformed task), that task is dropped from the summary
    rather than blowing up the whole record.
    """
    now = datetime.now(UTC)
    window_started = _as_aware_utc(since) or (now - _DEFAULT_MONITOR_WINDOW)

    completed: list[str] = []
    stalled: list[str] = []
    human_blockers: list[str] = []

    try:
        tasks = work_service.list_tasks(project=project_key)
    except Exception:  # noqa: BLE001
        tasks = []

    for task in tasks:
        try:
            status = getattr(getattr(task, "work_status", None), "value", "")
            task_id = getattr(task, "task_id", None) or ""
            updated_at = _as_aware_utc(getattr(task, "updated_at", None))
        except Exception:  # noqa: BLE001
            continue
        if not task_id:
            continue
        in_window = (
            isinstance(updated_at, datetime) and updated_at >= window_started
        )
        if status == "done" and _task_completed_in_window(task, window_started):
            completed.append(task_id)
        elif status in ("blocked", "on_hold"):
            human_blockers.append(task_id)
        elif status in ("waiting_on_user", "user_review"):
            human_blockers.append(task_id)
        elif status == "in_progress" and not in_window:
            # Updated outside the lookback window → stalled. A worker
            # actively turning on the task would have bumped
            # ``updated_at`` within the window.
            stalled.append(task_id)

    return ProjectMonitorSummary(
        project=project_key,
        completed_since_last=completed,
        stalled_tasks=stalled,
        human_blockers=human_blockers,
        automatic_next_actions=[],  # caller fills this in
        next_check_at=next_check_at,
        zero_completion_window=not completed,
        window_started_at=window_started.isoformat(),
    )


def record_project_blocker_summary(
    *,
    store: Any,
    work_service: Any,
    summary: ProjectBlockerSummary,
    actor: str = "polly",
) -> dict[str, Any]:
    """Persist a blocker summary and create a user task when needed."""
    payload = {
        "event_type": "project_blocker_summary",
        "project": summary.project,
        "reason": summary.reason,
        "owner": summary.owner,
        "required_actions": list(summary.required_actions),
        "affected_tasks": list(summary.affected_tasks),
        "unblock_condition": summary.unblock_condition,
    }
    event_id = store.record_event(
        summary.project,
        actor,
        "project.blocker_summary",
        payload=payload,
    )

    task_id: str | None = None
    if summary.owner.strip().lower() in {"user", "sam", "human"}:
        body = [
            summary.reason,
            "",
            "Required actions:",
            *[f"- {action}" for action in summary.required_actions],
            "",
            f"Unblock condition: {summary.unblock_condition or '(not specified)'}",
        ]
        task = work_service.create(
            title=f"Unblock {summary.project}",
            description="\n".join(body),
            type="task",
            project=summary.project,
            flow_template="chat",
            roles={"requester": "user", "operator": actor},
            priority="high",
            created_by=actor,
            labels=[
                "project_blocker",
                f"project:{summary.project}",
                f"blocker_event:{event_id}",
            ],
            requires_human_review=False,
        )
        task_id = task.task_id
        store.update_message(event_id, payload={**payload, "task_id": task_id})

    return {"event_id": event_id, "task_id": task_id}


def record_project_monitor_summary(
    *,
    store: Any,
    summary: ProjectMonitorSummary,
    actor: str = "advisor",
) -> int:
    """Record a passive monitor summary in the activity stream."""
    payload = {
        "event_type": "project_monitor_summary",
        "project": summary.project,
        "completed_since_last": list(summary.completed_since_last),
        "stalled_tasks": list(summary.stalled_tasks),
        "human_blockers": list(summary.human_blockers),
        "automatic_next_actions": list(summary.automatic_next_actions),
        "next_check_at": summary.next_check_at,
        # #782 explicit zero-completion + lookback window so renderers
        # can say "no completions in last 2h" without re-deriving.
        "zero_completion_window": summary.zero_completion_window,
        "window_started_at": summary.window_started_at,
    }
    return store.record_event(
        summary.project,
        actor,
        "project.monitor_summary",
        payload=payload,
    )
