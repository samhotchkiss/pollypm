"""Shared safeguards for cancelling active work tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pollypm.work.models import WorkStatus


def in_progress_assignee(task: Any) -> str | None:
    """Return the active assignee when cancel needs confirmation."""
    status = getattr(task, "work_status", None)
    if status is WorkStatus.IN_PROGRESS:
        return getattr(task, "assignee", None) or "unknown"
    value = getattr(status, "value", status)
    if value == WorkStatus.IN_PROGRESS.value:
        return getattr(task, "assignee", None) or "unknown"
    return None


def emit_cancel_safety_event(
    task: Any,
    *,
    event: str,
    actor: str,
    assignee: str,
    project_path: Path | str | None = None,
    surface: str | None = None,
    force: bool | None = None,
) -> None:
    """Best-effort audit breadcrumb for cancel warning/confirmation."""
    try:
        from pollypm.audit import emit as _audit_emit

        metadata: dict[str, object] = {"assignee": assignee}
        if surface is not None:
            metadata["surface"] = surface
        if force is not None:
            metadata["force"] = force
        _audit_emit(
            event=event,
            project=getattr(task, "project", ""),
            subject=getattr(task, "task_id", ""),
            actor=actor or "",
            metadata=metadata,
            project_path=project_path,
        )
    except Exception:  # noqa: BLE001 - audit must never block safety flow
        return
