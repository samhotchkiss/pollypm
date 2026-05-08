"""Compatibility API for ``task_assignment_notify``.

The shared task-assignment notification surface lives in
:mod:`pollypm.task_assignment_notify` (#1365). This plugin API remains for
existing imports, but core and peer plugins should import the shared module
directly instead of reaching through this plugin package.
"""

from __future__ import annotations

from typing import Any

from pollypm import task_assignment_notify as _shared


# Re-export plain constants directly — they don't need trampolining.
DEDUPE_WINDOW_SECONDS = _shared.DEDUPE_WINDOW_SECONDS
RECENT_SWEEPER_PING_SECONDS = _shared.RECENT_SWEEPER_PING_SECONDS
SWEEPER_PING_CONTEXT_ENTRY_TYPE = _shared.SWEEPER_PING_CONTEXT_ENTRY_TYPE


def load_runtime_services(*args: Any, **kwargs: Any) -> Any:
    return _shared.load_runtime_services(*args, **kwargs)


def notify(*args: Any, **kwargs: Any) -> Any:
    return _shared.notify(*args, **kwargs)


def clear_alerts_for_cancelled_task(*args: Any, **kwargs: Any) -> Any:
    return _shared.clear_alerts_for_cancelled_task(*args, **kwargs)


def clear_no_session_alert_for_task(*args: Any, **kwargs: Any) -> Any:
    return _shared.clear_no_session_alert_for_task(*args, **kwargs)


def auto_claim_enabled_for_project(*args: Any, **kwargs: Any) -> Any:
    return _shared.auto_claim_enabled_for_project(*args, **kwargs)


def build_event_for_task(*args: Any, **kwargs: Any) -> Any:
    return _shared.build_event_for_task(*args, **kwargs)


def close_quietly(*args: Any, **kwargs: Any) -> Any:
    return _shared.close_quietly(*args, **kwargs)


def open_project_work_service(*args: Any, **kwargs: Any) -> Any:
    return _shared.open_project_work_service(*args, **kwargs)


def record_sweeper_ping(*args: Any, **kwargs: Any) -> Any:
    return _shared.record_sweeper_ping(*args, **kwargs)


def recover_dead_claims(*args: Any, **kwargs: Any) -> Any:
    return _shared.recover_dead_claims(*args, **kwargs)


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
