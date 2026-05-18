"""Core seam for one-off invocation of recurring maintenance handlers.

Contract:
- Inputs: a handler name (``"agent_worktree.prune"`` or ``"log.rotate"``)
  plus a JSON-shaped payload dict.
- Outputs: the handler's result dict, or ``None`` when no provider is
  registered (plugin disabled / missing).
- Side effects: whatever the underlying handler does (filesystem prunes,
  log rotation). Handlers themselves are idempotent — invoking them when
  nothing's prunable is a cheap no-op.
- Invariants: when no provider is registered, callers see ``None`` and
  can degrade gracefully (the doctor fix-fn reports "handler unavailable"
  rather than crashing).

Boundary note:
This module is core — it must not import from ``pollypm.plugins_builtin``.
``doctor.py`` calls the registered handlers as part of its ``--fix`` flow
without taking a hard import on the optional ``core_recurring`` plugin.
The ``core_recurring`` plugin installs its ``agent_worktree_prune_handler``
and ``log_rotate_handler`` here during plugin ``initialize``.

Mirrors the registration-seam pattern established in
:mod:`pollypm.approval_notifications` (#1597) and
:mod:`pollypm.briefings_registry` (#1621). See #1363 for the full
boundary-debt roll-up.
"""

from __future__ import annotations

import logging
from typing import Any, Callable


logger = logging.getLogger(__name__)


MaintenanceHandler = Callable[[dict[str, Any]], dict[str, Any]]


# Handler names mirror the cadence-roster handler names the
# ``core_recurring`` plugin already registers with the job queue. We keep
# them as string constants so the doctor and the plugin can agree on the
# slot without sharing a Python import.
AGENT_WORKTREE_PRUNE = "agent_worktree.prune"
LOG_ROTATE = "log.rotate"


_handlers: dict[str, MaintenanceHandler] = {}


def register_maintenance_handler(
    name: str, handler: MaintenanceHandler | None
) -> None:
    """Install (or clear) a one-off maintenance handler under ``name``.

    Called by the ``core_recurring`` plugin during ``initialize`` so the
    doctor's ``--fix`` flow can invoke a handler immediately without
    waiting for the next cadence tick. Pass ``handler=None`` to clear
    (used by tests).
    """
    if handler is None:
        _handlers.pop(name, None)
        return
    _handlers[name] = handler


def get_maintenance_handler(name: str) -> MaintenanceHandler | None:
    """Return the handler registered under ``name`` or ``None``.

    Returning ``None`` is the documented "plugin disabled / not loaded"
    signal — callers must treat it as a degraded-but-OK state.
    """
    return _handlers.get(name)


def invoke_maintenance_handler(
    name: str, payload: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Invoke the handler registered under ``name``.

    Returns the handler's result dict on success, ``None`` when no
    handler is registered, and re-raises any exception raised by the
    handler so callers can surface the failure in their own format
    (e.g. the doctor's ``(ok, message)`` tuple).
    """
    handler = _handlers.get(name)
    if handler is None:
        logger.debug(
            "maintenance_handlers_registry: no provider for %s", name
        )
        return None
    return handler(payload or {})


__all__ = [
    "AGENT_WORKTREE_PRUNE",
    "LOG_ROTATE",
    "MaintenanceHandler",
    "get_maintenance_handler",
    "invoke_maintenance_handler",
    "register_maintenance_handler",
]
