"""Public-API re-exports for ``task_assignment_notify``.

The plugin's sweep helpers were originally private (underscore-prefixed)
implementation details. ``core_recurring.sweeps`` imports several of
them as part of the work-progress sweep pipeline, which made the two
plugins de-facto inseparable through a hidden private contract (#802).

Rather than duplicate the logic or collapse the plugins together, this
module promotes the cross-plugin dependencies to a documented public
surface. ``core_recurring`` (and any future caller that needs the same
hooks) imports from here; the underscored names remain in
``handlers/sweep.py`` for plugin-internal use, and can be refactored
without breaking external callers as long as the names re-exported
here keep their published shape.

Core runtime callers do not import this plugin surface (#1363). Neutral
task-assignment constants, event construction, and the dispatch bus live
in :mod:`pollypm.work.task_assignment`; this module remains the public
surface for peer built-in plugins that still need notifier-owned helpers.

If a future peer-plugin caller needs another helper, promote it here first,
then update the caller — never let core depend on this plugin's internals.

Each public name is implemented as a thin trampoline that resolves the
underlying private function via attribute lookup at call time. That
preserves test ergonomics: monkeypatching the source module
(``resolver`` / ``handlers.sweep``) propagates through the public
surface without callers having to know which physical module hosts
the implementation.
"""

from __future__ import annotations

from typing import Any

from pollypm.plugins_builtin.task_assignment_notify import resolver as _resolver
from pollypm.plugins_builtin.task_assignment_notify.handlers import (
    sweep as _sweep,
)


# Re-export plain constants directly — they don't need trampolining.
DEDUPE_WINDOW_SECONDS = _resolver.DEDUPE_WINDOW_SECONDS
RECENT_SWEEPER_PING_SECONDS = _sweep.RECENT_SWEEPER_PING_SECONDS
SWEEPER_PING_CONTEXT_ENTRY_TYPE = _sweep.SWEEPER_PING_CONTEXT_ENTRY_TYPE


def load_runtime_services(*args: Any, **kwargs: Any) -> Any:
    return _resolver.load_runtime_services(*args, **kwargs)


def notify(*args: Any, **kwargs: Any) -> Any:
    return _resolver.notify(*args, **kwargs)


def clear_alerts_for_cancelled_task(*args: Any, **kwargs: Any) -> Any:
    """Public re-export of the resolver helper used by the work-service
    cancel path (#927). Core must not import from
    :mod:`task_assignment_notify.resolver` directly — go through this
    surface so the plugin can refactor its internals freely."""
    return _resolver.clear_alerts_for_cancelled_task(*args, **kwargs)


def clear_no_session_alert_for_task(*args: Any, **kwargs: Any) -> Any:
    """Public re-export of the per-task no_session alert clear used by
    the work-service approve path (#953). Narrower than
    :func:`clear_alerts_for_cancelled_task` — only the per-task alert
    is cleared, since approve doesn't necessarily mean the role is no
    longer needed on the project."""
    return _resolver.clear_no_session_alert_for_task(*args, **kwargs)


def auto_claim_enabled_for_project(*args: Any, **kwargs: Any) -> Any:
    return _sweep._auto_claim_enabled_for_project(*args, **kwargs)


def build_event_for_task(*args: Any, **kwargs: Any) -> Any:
    return _sweep._build_event_for_task(*args, **kwargs)


def close_quietly(*args: Any, **kwargs: Any) -> Any:
    return _sweep._close_quietly(*args, **kwargs)


def open_project_work_service(*args: Any, **kwargs: Any) -> Any:
    return _sweep._open_project_work_service(*args, **kwargs)


def record_sweeper_ping(*args: Any, **kwargs: Any) -> Any:
    return _sweep._record_sweeper_ping(*args, **kwargs)


def recover_dead_claims(*args: Any, **kwargs: Any) -> Any:
    return _sweep._recover_dead_claims(*args, **kwargs)


__all__ = [
    "DEDUPE_WINDOW_SECONDS",
    "RECENT_SWEEPER_PING_SECONDS",
    "SWEEPER_PING_CONTEXT_ENTRY_TYPE",
    "auto_claim_enabled_for_project",
    "build_event_for_task",
    "clear_alerts_for_cancelled_task",
    "clear_no_session_alert_for_task",
    "close_quietly",
    "load_runtime_services",
    "notify",
    "open_project_work_service",
    "record_sweeper_ping",
    "recover_dead_claims",
]
