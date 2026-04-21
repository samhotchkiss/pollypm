"""Shared approval-notification helpers for human review surfaces.

Contract:
- Inputs: a reviewed ``Task`` plus a UI ``notify`` callback.
- Outputs: a stable approval toast message, and best-effort macOS banner
  delivery when Notification Center is available.
- Side effects: emits a Textual toast immediately and may queue an OS
  notification on Darwin hosts.
- Invariants: approval flows format the same message everywhere and never
  fail the underlying approval action when notification delivery flakes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from pollypm.plugins_builtin.human_notify.macos import MacOsNotifyAdapter

if TYPE_CHECKING:
    from pollypm.plugins_builtin.human_notify.protocol import HumanNotifyAdapter
    from pollypm.work.models import Task


NotifyCallback = Callable[..., None]
_TOAST_TIMEOUT_SECONDS = 5.0


def format_task_approval_message(
    task: "Task",
    *,
    approved_at: datetime | None = None,
) -> str:
    """Build the shared celebratory approval message."""
    shipped_in = _format_elapsed(
        getattr(task, "created_at", None),
        approved_at or datetime.now(UTC),
    )
    if shipped_in:
        return f"\u2713 {task.task_id} approved - shipped in {shipped_in}"
    return f"\u2713 {task.task_id} approved"


def notify_task_approved(
    task: "Task",
    *,
    notify: NotifyCallback,
    approved_at: datetime | None = None,
    os_adapter: "HumanNotifyAdapter | None" = None,
) -> str:
    """Emit the in-cockpit toast and best-effort macOS banner."""
    message = format_task_approval_message(task, approved_at=approved_at)
    notify(message, severity="information", timeout=_TOAST_TIMEOUT_SECONDS)

    adapter = os_adapter or MacOsNotifyAdapter()
    try:
        available = adapter.is_available()
    except Exception:  # noqa: BLE001
        available = False
    if available:
        adapter.notify(
            title="PollyPM: Task approved",
            body=message,
            task_id=task.task_id,
            project=task.project,
        )
    return message


def _format_elapsed(
    started_at: datetime | None,
    finished_at: datetime,
) -> str | None:
    """Render a compact elapsed duration like ``45m`` or ``2h 5m``."""
    if started_at is None:
        return None
    start = _coerce_utc(started_at)
    end = _coerce_utc(finished_at)
    if start is None or end is None:
        return None
    total_seconds = int((end - start).total_seconds())
    if total_seconds <= 0:
        return None

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "format_task_approval_message",
    "notify_task_approved",
]
