"""Shared approval-notification helpers for human review surfaces.

Contract:
- Inputs: a reviewed ``Task`` plus a UI ``notify`` callback.
- Outputs: a stable approval toast message, and best-effort OS banner
  delivery when a notification adapter has been registered (typically
  by the ``human_notify`` plugin).
- Side effects: emits a Textual toast immediately and may queue an OS
  notification when an adapter is registered.
- Invariants: approval flows format the same message everywhere and never
  fail the underlying approval action when notification delivery flakes.

Boundary note:
This module is core — it must not import from ``pollypm.plugins_builtin``.
The OS-level adapter is resolved through :func:`register_default_os_adapter`
so the ``human_notify`` plugin (or any third-party replacement) can plug
in without core taking a hard dependency on the optional plugin tree.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pollypm.work.models import Task

logger = logging.getLogger(__name__)


NotifyCallback = Callable[..., None]
_TOAST_TIMEOUT_SECONDS = 5.0


@runtime_checkable
class OsApprovalNotifier(Protocol):
    """Minimal OS-notification contract for the approval flow.

    Mirrors the public surface of the ``human_notify`` plugin's
    ``HumanNotifyAdapter`` but lives in core so this module doesn't
    reach into the plugin tree. Plugin adapters that already satisfy
    ``HumanNotifyAdapter`` structurally satisfy this Protocol too.
    """

    def is_available(self) -> bool:
        """Return True iff this adapter can deliver a banner right now."""
        ...

    def notify(
        self,
        *,
        title: str,
        body: str,
        task_id: str,
        project: str,
    ) -> None:
        """Deliver a single approval banner."""
        ...


# Module-level registration slot. The ``human_notify`` plugin (or a
# replacement) installs its OS adapter factory here during plugin
# ``initialize``; when nothing is registered, the approval flow still
# emits the in-cockpit toast but skips the OS banner.
_default_os_adapter_factory: Callable[[], OsApprovalNotifier] | None = None


def register_default_os_adapter(
    factory: Callable[[], OsApprovalNotifier] | None,
) -> None:
    """Install (or clear) the default OS-notification adapter factory.

    Called by the ``human_notify`` plugin during ``initialize`` so
    ``notify_task_approved`` can deliver banners without a hard import
    on the plugin tree. Pass ``None`` to clear (used by tests).
    """
    global _default_os_adapter_factory
    _default_os_adapter_factory = factory


def _resolve_default_os_adapter() -> OsApprovalNotifier | None:
    factory = _default_os_adapter_factory
    if factory is None:
        return None
    try:
        return factory()
    except Exception:  # noqa: BLE001
        # #1355: previously silent. A broken factory disables OS banners
        # for every approval — log so the plugin owner can spot the
        # regression without grepping for missing toasts.
        logger.warning(
            "approval_notifications: default OS adapter factory raised; "
            "OS banners disabled for this approval",
            exc_info=True,
        )
        return None


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
        return f"✓ {task.task_id} approved - shipped in {shipped_in}"
    return f"✓ {task.task_id} approved"


def notify_task_approved(
    task: "Task",
    *,
    notify: NotifyCallback,
    approved_at: datetime | None = None,
    os_adapter: "OsApprovalNotifier | None" = None,
) -> str:
    """Emit the in-cockpit toast and best-effort OS banner.

    The OS banner is delivered through ``os_adapter`` when the caller
    supplies one (tests, custom flows); otherwise the registered
    default adapter is consulted. With no registered adapter the
    banner is skipped silently — the toast still fires.
    """
    message = format_task_approval_message(task, approved_at=approved_at)
    notify(message, severity="information", timeout=_TOAST_TIMEOUT_SECONDS)

    adapter = os_adapter if os_adapter is not None else _resolve_default_os_adapter()
    if adapter is None:
        return message
    try:
        available = adapter.is_available()
    except Exception:  # noqa: BLE001
        # #1355: previously silent. Treat adapter probe failures as
        # unavailable but log — a flaky is_available masks every
        # subsequent banner without trace.
        logger.warning(
            "approval_notifications: OS adapter is_available() raised for %s; "
            "treating as unavailable",
            getattr(adapter, "__class__", type(adapter)).__name__,
            exc_info=True,
        )
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
    "OsApprovalNotifier",
    "format_task_approval_message",
    "notify_task_approved",
    "register_default_os_adapter",
]
