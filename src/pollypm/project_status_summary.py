"""Project blocker and monitoring summaries.

These helpers keep the product surfaces separate:
- activity/dashboard summaries are durable Store events;
- user action is materialized as a work-service task assigned to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    }
    return store.record_event(
        summary.project,
        actor,
        "project.monitor_summary",
        payload=payload,
    )
