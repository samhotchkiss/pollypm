"""Built-in ``task_assignment_notify`` plugin.

Subscribes to the in-process ``TaskAssignmentEvent`` bus (fed by
``SQLiteWorkService._sync_transition``) and registers a ``@every 30s``
sweeper so events dropped on restart or emitted before a target session
exists still get delivered.

Design: see issue #244 and ``pollypm.work.task_assignment``. No
SessionService protocol change — the target session is resolved purely
by naming convention (``worker-<project>`` / ``worker_<project>``,
``pm-reviewer``, etc.).

Also subscribes to ``SessionCreatedEvent`` (#246): when a fresh session
comes online, replay any outstanding queued / in_progress / review
pings that target it. This closes the supervisor-restart resume gap
without waiting on the 30-second sweeper cadence — dedupe still
applies, so re-pings inside the 30-minute window are suppressed.
"""

from __future__ import annotations

import logging

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
    _build_event_for_task,
    task_assignment_sweep_handler,
)
from pollypm.plugins_builtin.task_assignment_notify.resolver import (
    DEDUPE_WINDOW_SECONDS,
    clear_alerts_for_cancelled_task,
    clear_no_session_alert_for_task,
    load_runtime_services,
    notify,
)
from pollypm.session_services import base as _session_bus
from pollypm.work import task_assignment_alerts as _assignment_alert_bus
from pollypm.work import task_assignment as _task_assignment_bus

logger = logging.getLogger(__name__)

# Statuses we replay for a freshly-created session. ``in_progress`` is
# the #246 headline — a restarted worker gets its resume ping immediately
# instead of after a sweeper cycle. queued / review covers the slower
# case where a pre-existing assignment was waiting for the session.
_REPLAY_STATUSES = ("in_progress", "queued", "review")


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


def _session_created_listener(event) -> None:
    """Replay outstanding pings for tasks targeting a just-created session.

    For every task in ``queued`` / ``review`` / ``in_progress`` whose
    resolved target matches ``event.name``, emit a notify event right
    now. This is the #246 fix — resume-ping the new worker immediately
    on supervisor restart rather than waiting for the next sweeper tick.

    Dedupe (30-min per (session, task)) still applies via ``notify()``
    so repeated session births within the window don't spam.
    """
    services = None
    try:
        services = load_runtime_services()
        work = services.work_service
        session_svc = services.session_service
        if work is None or session_svc is None:
            logger.debug(
                "task_assignment_notify: session.created — no work/session service; skipping",
            )
            return

        # Import here to avoid a module-level cycle with the sweep handler.
        from pollypm.work.task_assignment import SessionRoleIndex

        index = SessionRoleIndex(session_svc, work_service=work)
        target_name = event.name

        for status in _REPLAY_STATUSES:
            try:
                tasks = work.list_tasks(work_status=status)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "task_assignment_notify: list_tasks(%s) failed", status,
                    exc_info=True,
                )
                continue
            for task in tasks:
                task_event = _build_event_for_task(work, task)
                if task_event is None:
                    continue
                handle = index.resolve(
                    task_event.actor_type,
                    task_event.actor_name,
                    task_event.project,
                    task_number=task_event.task_number,
                )
                if handle is None:
                    continue
                if getattr(handle, "name", "") != target_name:
                    continue
                try:
                    notify(
                        task_event,
                        services=services,
                        throttle_seconds=DEDUPE_WINDOW_SECONDS,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "task_assignment_notify: notify failed for %s",
                        task_event.task_id, exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        logger.exception(
            "task_assignment_notify: session.created replay failed for %s",
            getattr(event, "name", "?"),
        )
    finally:
        if services is not None:
            closer = getattr(services.work_service, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass


def _assignment_alert_cleanup_listener(
    event: _assignment_alert_bus.TaskAssignmentAlertEvent,
) -> None:
    """Clear assignment alerts made stale by work-service transitions."""
    if isinstance(event, _assignment_alert_bus.CancelledTaskAssignmentAlertsEvent):
        clear_alerts_for_cancelled_task(
            task_id=event.task_id,
            project=event.project,
            role_names=tuple(event.role_names),
            has_other_active_for_role=dict(event.has_other_active_for_role),
            store=event.store,
        )
        return
    if isinstance(event, _assignment_alert_bus.ClearNoSessionAlertForTaskEvent):
        clear_no_session_alert_for_task(
            task_id=event.task_id,
            store=event.store,
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
    # after the original transition. #1052 — dedupe_key prevents the
    # @every 30s tick from compounding queued rows when the worker pool
    # falls behind under contention.
    api.register_recurring(
        "@every 30s", "task_assignment.sweep", {},
        dedupe_key="task_assignment.sweep",
    )


def _initialize(api: PluginAPI) -> None:
    """Wire the event bus + roster registration.

    Called exactly once per process after all plugins are loaded. The
    listener registration is idempotent so repeated initialize() calls
    (e.g. in tests that recycle the plugin host) don't stack duplicate
    subscribers.
    """
    _task_assignment_bus.register_listener(_in_process_listener)
    _assignment_alert_bus.register_listener(_assignment_alert_cleanup_listener)
    # #246: subscribe to session births so a restarted worker gets its
    # resume ping inside ~1s instead of on the next sweeper cycle.
    _session_bus.register_session_listener(_session_created_listener)
    try:
        api.roster.register_recurring(
            "@every 30s", "task_assignment.sweep", {},
            dedupe_key="task_assignment.sweep",
        )
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
