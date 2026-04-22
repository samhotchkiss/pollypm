"""Small offline demo for PollyPM onboarding."""

from __future__ import annotations


def summarize_queue(items: list[str]) -> str:
    cleaned = [item.strip() for item in items if item.strip()]
    if not cleaned:
        return "No tasks queued."
    if len(cleaned) == 1:
        return f"1 task queued: {cleaned[0]}"
    preview = ", ".join(cleaned[:2])
    if len(cleaned) > 2:
        preview += f", +{len(cleaned) - 2} more"
    return f"{len(cleaned)} tasks queued: {preview}"


def estimate_focus_minutes(task_count: int, *, per_task: int = 25) -> int:
    """Return a rough focus block estimate for the queue.

    The demo task deliberately asks the user to fix the single-item
    estimate bug in the follow-up regression test.
    """
    if task_count <= 0:
        return 0
    return task_count * per_task
