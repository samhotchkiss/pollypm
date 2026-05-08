"""Emit a `worker_milestone` inbox card from a worker mid-task.

Workers iterate silently between `pm task claim` and `pm task done`. The
operator (Polly) sees the start and the finish but nothing in between —
"render started", "research done", "deploy started" all happen inside
the worker pane and never surface unless the operator asks. That gap is
why coffeeboardnm/1 looked stalled for 14 minutes during the visual-explainer
render this past session: real progress was happening; nobody could see it.

This module provides the emit hook backing the ``pm send-up`` CLI: a worker
can call ``pm send-up "outline drafted, starting render"`` and a single
inbox card lands in the operator's inbox with that message.

Boundary discipline: this module imports only from
:mod:`pollypm.work.models` (typed value objects). It does not import any
plugin, does not reach into private store internals, and does not depend
on a CLI module.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_WORKER_MILESTONE_LABEL = "worker_milestone"

# Title summary cap so the inbox subject column stays readable. Full
# message goes in the body.
_TITLE_SUMMARY_LEN = 80

# Dedupe window: a worker hammering ``pm send-up`` on the same message
# (e.g. retry after a transient error) shouldn't spam the operator.
# 5 minutes is short enough to allow legitimate "still rendering"
# follow-ups and long enough to suppress accidental repeats.
_DEDUPE_WINDOW_SECONDS = 300


class _WorkerMilestoneSvc(Protocol):
    """Minimal contract for the work service surface this module touches."""

    def get(self, task_id: str) -> Any: ...

    def list_tasks(self, **kwargs: Any) -> list[Any]: ...

    def create(self, **kwargs: Any) -> Any: ...


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _build_milestone_title(task_id: str, message: str) -> str:
    """Build a one-line subject for the inbox card."""
    summary = _truncate(message, _TITLE_SUMMARY_LEN)
    return f"{task_id}: {summary}"


def _build_milestone_body(task: Any, message: str) -> str:
    """Build the inbox card body."""
    parts: list[str] = []
    title = (getattr(task, "title", "") or "").strip()
    if title:
        parts.append(f"**Task:** {task.task_id} — {title}")
    else:
        parts.append(f"**Task:** {task.task_id}")
    parts.append("")
    parts.append(message.strip())
    return "\n".join(parts)


def _is_recent_duplicate(
    svc: _WorkerMilestoneSvc,
    task_id: str,
    message: str,
    *,
    now_seconds: float,
    window_seconds: float = _DEDUPE_WINDOW_SECONDS,
) -> bool:
    """Return True iff the same message was already emitted for ``task_id``
    within ``window_seconds``.

    Looks at recent tasks under the same project carrying the
    ``worker_milestone`` label and compares description payloads. Does
    not raise — a query failure returns False (favor emit over silent
    drop).
    """
    project = task_id.split("/", 1)[0] if "/" in task_id else None
    if not project:
        return False
    try:
        recent = svc.list_tasks(project=project, limit=20)
    except Exception:  # noqa: BLE001
        return False
    needle = message.strip()
    for entry in recent:
        labels = getattr(entry, "labels", None) or []
        if _WORKER_MILESTONE_LABEL not in labels:
            continue
        # The card's body has the task_id + needle; compare on the
        # message tail so a mismatched task title doesn't defeat
        # dedup.
        body = (getattr(entry, "description", "") or "")
        if needle not in body:
            continue
        # Compare timestamps if the entry has a created_at; if it
        # doesn't, assume "recent enough" since list_tasks ordered
        # newest-first by convention.
        created_at = getattr(entry, "created_at", None)
        if created_at is None:
            return True
        try:
            created_seconds = (
                created_at.timestamp() if hasattr(created_at, "timestamp")
                else float(created_at)
            )
        except Exception:  # noqa: BLE001
            return True
        if (now_seconds - created_seconds) <= window_seconds:
            return True
    return False


def emit_worker_milestone(
    svc: _WorkerMilestoneSvc,
    task_id: str,
    message: str,
    *,
    actor: str | None = None,
    now_seconds: float | None = None,
) -> str | None:
    """Emit a ``worker_milestone`` inbox card. Returns new card id, or None.

    Returns None on:
    - empty/whitespace ``message``
    - task lookup failure (the task doesn't exist or the store is
      transiently unavailable)
    - recent-duplicate detection
    - the create call itself failing

    All failures are logged at debug — this is best-effort
    progress-reporting, never a blocker for the worker's actual work.
    """
    text = (message or "").strip()
    if not text:
        return None
    try:
        try:
            task = svc.get(task_id)
        except Exception:  # noqa: BLE001
            return None

        clock_seconds = now_seconds if now_seconds is not None else time.time()
        if _is_recent_duplicate(svc, task_id, text, now_seconds=clock_seconds):
            logger.debug(
                "worker_milestone dedupe skip for %s: %r", task_id, text[:60]
            )
            return None

        title = _build_milestone_title(task.task_id, text)
        body = _build_milestone_body(task, text)

        # operator is fixed: milestones always surface to polly. The
        # caller-controlled identity is ``created_by`` (which worker
        # said it).
        card = svc.create(
            title=title,
            description=body,
            type="task",
            project=task.project,
            flow_template="chat",
            roles={"requester": "worker", "operator": "polly"},
            priority="normal",
            created_by=actor or "worker",
            labels=[_WORKER_MILESTONE_LABEL],
        )
        return getattr(card, "task_id", None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "worker_milestone emit skipped for %s: %s",
            task_id,
            exc,
            exc_info=True,
        )
        return None


__all__ = ["emit_worker_milestone"]
