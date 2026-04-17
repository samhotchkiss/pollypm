"""Built-in ``task_assignment_notify`` plugin.

Subscribes to the in-process ``TaskAssignmentEvent`` bus (fed by
``SQLiteWorkService._sync_transition``) and registers a ``@every 30s``
sweeper so events dropped on restart or emitted before a target session
exists still get delivered.

Design: see issue #244 and ``pollypm.work.task_assignment``. No
SessionService protocol change — the target session is resolved purely
by naming convention (``worker-<project>`` / ``worker_<project>``,
``pm-reviewer``, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from pollypm.plugin_api.v1 import (
    Capability,
    JobHandlerAPI,
    PluginAPI,
    PollyPMPlugin,
    RosterAPI,
)
from pollypm.plugins_builtin.task_assignment_notify.handlers.notify import (
    event_to_payload,
    task_assignment_notify_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
    task_assignment_sweep_handler,
)
from pollypm.work import task_assignment as _task_assignment_bus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event listener — dispatched inside the work service's _sync_transition.
# ---------------------------------------------------------------------------


def _in_process_listener(event) -> None:
    """Handle a ``TaskAssignmentEvent`` in the same process.

    The naive implementation would enqueue a ``task_assignment.notify``
    job and let the job runner pick it up. That's correct for remote
    deployments, but the common local setup runs the work service and
    the session service in the same process — so we invoke the notify
    handler synchronously for zero-latency delivery.

    Exceptions are swallowed by the bus's ``dispatch`` wrapper; we log
    here for observability.
    """
    try:
        task_assignment_notify_handler(event_to_payload(event))
    except Exception:  # noqa: BLE001
        logger.exception(
            "task_assignment_notify: in-process listener failed for %s",
            getattr(event, "task_id", "?"),
        )


def _register_handlers(api: JobHandlerAPI) -> None:
    # Notify is idempotent (dedupe table throttles duplicate pings), so
    # we give it a generous retry count. The sweeper is a @every job so
    # we cap attempts low — it re-runs on the next tick anyway.
    api.register_handler(
        "task_assignment.notify", task_assignment_notify_handler,
        max_attempts=3, timeout_seconds=30.0,
    )
    api.register_handler(
        "task_assignment.sweep", task_assignment_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )


def _register_roster(api: RosterAPI) -> None:
    # 30-second sweep catches events dropped on restart, pre-existing
    # queued tasks at plugin install, and sessions that came online
    # after the original transition.
    api.register_recurring("@every 30s", "task_assignment.sweep", {})


def _initialize(api: PluginAPI) -> None:
    """Wire the event bus + roster registration.

    Called exactly once per process after all plugins are loaded. The
    listener registration is idempotent so repeated initialize() calls
    (e.g. in tests that recycle the plugin host) don't stack duplicate
    subscribers.
    """
    _task_assignment_bus.register_listener(_in_process_listener)
    try:
        api.roster.register_recurring("@every 30s", "task_assignment.sweep", {})
    except RuntimeError:
        # No roster rail in this context (some test harnesses skip it).
        logger.debug(
            "task_assignment_notify: initialize skipped roster registration — no RosterAPI"
        )


plugin = PollyPMPlugin(
    name="task_assignment_notify",
    version="0.1.0",
    description=(
        "Event-driven + roster-sweep notifier that pings the right session "
        "when a task transitions to a non-user actor (worker, reviewer, "
        "operator, critic, etc.). Closes the idle-recipient gap from #244."
    ),
    capabilities=(
        Capability(kind="job_handler", name="task_assignment.notify"),
        Capability(kind="job_handler", name="task_assignment.sweep"),
        Capability(kind="roster_entry", name="task_assignment.sweep"),
    ),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
    initialize=_initialize,
)
