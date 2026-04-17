"""``inbox_notification_sent`` — hard gate for the downtime flow.

Sits on the ``awaiting_approval`` node of ``downtime_explore``. The flow
cannot advance past ``explore`` until the downtime handler has actually
dispatched the inbox notification that lets the user decide whether to
approve or reject.

Why it exists: the task is *already* in the inbox the moment its current
node's ``actor_type`` becomes ``human`` — that's the inbox view's
membership rule (see ``pollypm.work.inbox_view``). But the spec §7
contract is stronger: the downtime flow must explicitly write a
downtime-result notification (kind=``downtime_result``) with a summary +
artifact pointers before we block on the user. This gate verifies that
an explicit notification was emitted — it does not trust implicit
inbox-view membership.

Detection signal: the handler appends a ``ContextEntry`` to the task
with text starting ``inbox_notification_sent:`` (or
``[downtime] inbox_notification_sent``) when it dispatches the
notification. dt06 owns the actual write path; dt02 only establishes
the gate contract.
"""
from __future__ import annotations

from typing import Any

from pollypm.work.models import GateResult, Task


# Marker prefixes the downtime notification handler writes to the task
# context when it dispatches the inbox entry. The gate matches either
# form so the handler has room to evolve its log text.
_MARKERS: tuple[str, ...] = (
    "inbox_notification_sent",
    "[downtime] inbox_notification_sent",
)


class InboxNotificationSent:
    """Hard gate — fails until the handler has logged the notification."""

    name = "inbox_notification_sent"
    gate_type = "hard"

    def check(self, task: Task, **kwargs: Any) -> GateResult:
        for entry in reversed(task.context or []):
            text = getattr(entry, "text", "") or ""
            stripped = text.strip()
            for marker in _MARKERS:
                if stripped.startswith(marker):
                    return GateResult(
                        passed=True,
                        reason=(
                            "Downtime inbox notification has been dispatched "
                            "(context log entry present)."
                        ),
                    )
        return GateResult(
            passed=False,
            reason=(
                "Downtime inbox notification has not been dispatched yet. "
                "The downtime plugin must emit a `downtime_result` inbox "
                "message (and log `inbox_notification_sent` to the task "
                "context) before this node can advance."
            ),
        )
