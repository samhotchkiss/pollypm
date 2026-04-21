"""Structured rejection-feedback artifacts for cockpit task/inbox surfaces.

Contract:
- Inputs: a rejected work task plus reviewer + reason strings, or a
  work service that can list tasks and read task context.
- Outputs: inbox-task payloads and unread-feedback indexes keyed by the
  original task id.
- Side effects: optional inbox-task creation via the passed work service.
- Invariants: feedback artifacts are ordinary chat-flow inbox tasks
  labeled with ``review_feedback`` and ``task:<project/number>`` so the
  task UI and inbox UI can consume one stable contract.
"""

from __future__ import annotations

from dataclasses import dataclass

FEEDBACK_LABEL = "review_feedback"
TASK_LABEL_PREFIX = "task:"
PROJECT_LABEL_PREFIX = "project:"


@dataclass(slots=True)
class RejectionFeedbackNotice:
    """Unread rejection feedback linked to one work task."""

    task_id: str
    inbox_task_id: str
    preview: str
    created_at: object | None = None


def emit_rejection_feedback(work_service, *, task, reviewer: str, reason: str):
    """Create a chat-flow inbox task capturing one rejection reason."""

    reviewer_name = _reviewer_label(reviewer)
    preview = rejection_feedback_preview_from_reason(reason)
    body = "\n".join(
        [
            preview,
            "",
            f"Task `{task.task_id}` was rejected by `{reviewer_name}` and returned to rework.",
            "",
            f"Current stage: `{getattr(task, 'current_node_id', None) or 'unknown'}`",
            "",
            "Open the linked task in the task cockpit, or jump to the inbox thread to",
            "review the full rejection note before the worker continues.",
        ]
    )
    return work_service.create(
        title=f"Rejected {task.task_id} — {getattr(task, 'title', '') or 'Untitled task'}",
        description=body,
        type="task",
        project=getattr(task, "project", "") or "inbox",
        flow_template="chat",
        roles={"requester": reviewer_name, "operator": "user"},
        priority="high",
        created_by=reviewer_name,
        labels=[
            FEEDBACK_LABEL,
            f"{TASK_LABEL_PREFIX}{task.task_id}",
            f"{PROJECT_LABEL_PREFIX}{getattr(task, 'project', '') or 'inbox'}",
        ],
    )


def unread_rejection_feedback(service, *, project: str | None = None) -> dict[str, RejectionFeedbackNotice]:
    """Return latest unread rejection feedback, keyed by target task id."""

    notices: dict[str, RejectionFeedbackNotice] = {}
    for task in service.list_tasks(project=project):
        if not is_rejection_feedback_task(task):
            continue
        if _task_has_read_marker(service, task.task_id):
            continue
        target_task_id = feedback_target_task_id(task)
        if not target_task_id:
            continue
        notice = RejectionFeedbackNotice(
            task_id=target_task_id,
            inbox_task_id=task.task_id,
            preview=rejection_feedback_preview(task),
            created_at=getattr(task, "updated_at", None) or getattr(task, "created_at", None),
        )
        current = notices.get(target_task_id)
        if current is None or _sort_timestamp(notice.created_at) >= _sort_timestamp(current.created_at):
            notices[target_task_id] = notice
    return notices


def is_rejection_feedback_task(task) -> bool:
    """True when ``task`` is a structured review-feedback inbox item."""

    labels = list(getattr(task, "labels", []) or [])
    return FEEDBACK_LABEL in labels and any(
        label.startswith(TASK_LABEL_PREFIX) for label in labels
    )


def feedback_target_task_id(task) -> str | None:
    """Extract the linked work-task id from a feedback inbox task."""

    for label in list(getattr(task, "labels", []) or []):
        if label.startswith(TASK_LABEL_PREFIX):
            value = label[len(TASK_LABEL_PREFIX):].strip()
            return value or None
    return None


def rejection_feedback_preview(task) -> str:
    """Best-effort first-line preview for an inbox feedback artifact."""

    description = (getattr(task, "description", "") or "").strip()
    if description:
        for line in description.splitlines():
            line = line.strip()
            if line:
                return line
    title = (getattr(task, "title", "") or "").strip()
    if " — " in title:
        return title.split(" — ", 1)[1].strip() or title
    return title or "Rejected — feedback in inbox"


def rejection_feedback_preview_from_reason(reason: str) -> str:
    """First non-empty line of a rejection reason, normalized for UI use."""

    for line in (reason or "").splitlines():
        line = line.strip()
        if line:
            return line
    return "Rejected — feedback in inbox"


def _reviewer_label(reviewer: str) -> str:
    label = (reviewer or "").strip()
    if not label or label == "user":
        return "reviewer"
    return label


def _task_has_read_marker(service, task_id: str) -> bool:
    try:
        rows = service.get_context(task_id, entry_type="read", limit=1)
    except TypeError:
        try:
            rows = service.get_context(task_id, limit=1)
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False
    return bool(rows)


def _sort_timestamp(value: object | None) -> float:
    if value is None:
        return 0.0
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        from datetime import datetime as _dt

        return float(_dt.fromisoformat(str(value)).timestamp())
    except Exception:  # noqa: BLE001
        return 0.0


__all__ = [
    "FEEDBACK_LABEL",
    "PROJECT_LABEL_PREFIX",
    "RejectionFeedbackNotice",
    "TASK_LABEL_PREFIX",
    "emit_rejection_feedback",
    "feedback_target_task_id",
    "is_rejection_feedback_task",
    "rejection_feedback_preview",
    "rejection_feedback_preview_from_reason",
    "unread_rejection_feedback",
]
