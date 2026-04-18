"""Built-in recurring handlers migrated from the old heartbeat loop.

Previously, the supervisor dispatched recurring work (inbox sweep, capacity
probe, session health sweep, transcript ingest, alert GC) on each tick via
direct function calls. Track 7 moves these onto the durable roster + job
queue so the heartbeat's entire responsibility is to enqueue and a separate
worker pool drains the queue.

Handlers live as module-level callables taking a payload dict; plugins
register them via ``register_handlers`` and register cadence via
``register_roster``. See issue #164.

Handlers must be self-sufficient — they receive only a JSON-serializable
payload, so each one loads the shared PollyPM config internally. The payload
may carry per-invocation hints (e.g. ``project_root`` overrides).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pollypm.plugin_api.v1 import Capability, JobHandlerAPI, PollyPMPlugin, RosterAPI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handlers — each is a standalone callable, tolerant of partial config.
# ---------------------------------------------------------------------------


def _load_config(payload: dict[str, Any]):
    """Resolve + load the PollyPM config for a handler invocation.

    Handlers accept an optional ``config_path`` override in ``payload`` so
    tests (and alt installations) can target a non-default config. Falls
    back to the global default discovery.

    Use this helper when the handler does NOT need a ``StateStore`` — it
    avoids opening a SQLite file descriptor that would then leak on the
    recurring schedule (the heartbeat fires some handlers every ~10s).
    """
    from pollypm.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path

    override = payload.get("config_path") if isinstance(payload, dict) else None
    config_path = Path(override) if override else resolve_config_path(DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise RuntimeError(
            f"PollyPM config not found at {config_path}; cannot run recurring handler"
        )
    return load_config(config_path)


@contextmanager
def _load_config_and_store(payload: dict[str, Any]):
    """Context-managed config + state store for handler invocations.

    Yields ``(config, store)``. The store is closed deterministically on
    exit — callers MUST use
    ``with _load_config_and_store(payload) as (config, store): ...``.

    Recurring handlers fire every 10-60s, so relying on garbage collection
    to close the underlying SQLite connection leaks file descriptors over
    hours and eventually trips ``OSError: [Errno 24] Too many open
    files``. The explicit ``finally: store.close()`` below is the fix.
    """
    from pollypm.storage.state import StateStore

    config = _load_config(payload)
    store = StateStore(config.project.state_db)
    try:
        yield config, store
    finally:
        store.close()


def _open_msg_store(config: Any) -> Any:
    """Open the unified-messages Store for a handler invocation (#349).

    Returns ``None`` when the backend can't be resolved — callers treat
    that as a soft skip so a misconfigured entry-point never crashes a
    recurring handler.
    """
    try:
        from pollypm.store.registry import get_store

        return get_store(config)
    except Exception:  # noqa: BLE001
        logger.debug(
            "core_recurring: unified Store unavailable", exc_info=True,
        )
        return None


def _close_msg_store(store: Any) -> None:
    """Close a Store handle opened by :func:`_open_msg_store`.

    Tolerant of ``None`` (no-op) and missing ``close`` (some test doubles
    don't implement it).
    """
    if store is None:
        return
    close = getattr(store, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.debug("core_recurring: msg_store close raised", exc_info=True)


# Ephemeral session name prefixes (#252). Sessions whose name starts with
# any of these are NOT in the supervisor's launch plan — they're spawned
# on-demand for a specific task / critic pass / downtime exploration. The
# health sweep classifies them with ``is_ephemeral=True`` so interventions
# differ: we surface failure to the parent task instead of restarting.
_EPHEMERAL_SESSION_PREFIXES: tuple[str, ...] = (
    "task-",
    "critic_",
    "downtime_",
)


def is_ephemeral_session_name(name: str) -> bool:
    """Return True if ``name`` matches an ephemeral session naming convention.

    Ephemeral sessions are spawned on-demand by the work service / planners
    rather than declared in the supervisor's launch plan. Examples:

    * ``task-<project>-<number>`` — per-task worker session.
    * ``critic_<flavor>`` — per-task critic pass (security, simplicity, …).
    * ``downtime_<slug>`` — speculative downtime exploration session.

    A name with no prefix match returns False — planned sessions
    (``operator``, ``architect-<project>``, ``worker-<project>``, etc.)
    are never classified as ephemeral.
    """
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in _EPHEMERAL_SESSION_PREFIXES)


def session_health_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one round of session health classification.

    Mirrors the supervisor's Phase 2 "fast synchronous sweep" — builds the
    ``SupervisorHeartbeatAPI``, invokes the configured heartbeat backend,
    collects alerts. Returns a small summary.

    The supervisor still owns the tmux-touching pieces, so this handler
    instantiates a transient ``Supervisor`` bound to the current config.
    Works for the co-located single-process setup; plugin overlays can
    replace this with a network-aware implementation.

    After the planned-session sweep we run a second pass over *ephemeral*
    sessions — ``task-*``, ``critic_*``, ``downtime_*`` — that the
    launch planner doesn't know about (#252). Their classification carries
    ``is_ephemeral=True`` and the intervention dispatch is restricted to
    raising an alert tied to the parent task; we never auto-restart an
    ephemeral session because its lifecycle is owned by whoever spawned it.
    """
    with _load_config_and_store(payload) as (config, store):
        # Late import to avoid a supervisor import cycle at plugin load.
        from pollypm.supervisor import Supervisor

        supervisor = Supervisor(config)
        alerts = supervisor.run_heartbeat(snapshot_lines=int(payload.get("snapshot_lines", 200) or 200))

        # #252 — ephemeral session sweep. Best-effort: any failure is logged
        # but never fails the planned-session sweep result, which is the
        # caller's primary contract.
        ephemeral_summary = {
            "considered": 0, "alerts_raised": 0, "skipped_planned": 0,
        }
        try:
            # #349: the ephemeral sweep emits alerts through the unified
            # Store. Route through the supervisor's ``_msg_store`` so the
            # writer lands on ``messages`` rather than the legacy table.
            msg_store = getattr(supervisor, "_msg_store", None)
            ephemeral_summary = sweep_ephemeral_sessions(
                supervisor, msg_store or store,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "session.health_sweep: ephemeral pass failed", exc_info=True,
            )

        return {
            "alerts_raised": len(alerts),
            "ephemeral_considered": ephemeral_summary["considered"],
            "ephemeral_alerts_raised": ephemeral_summary["alerts_raised"],
            "ephemeral_skipped_planned": ephemeral_summary["skipped_planned"],
        }


def sweep_ephemeral_sessions(supervisor: Any, store: Any) -> dict[str, int]:
    """Mechanical health pass over ephemeral (non-planned) sessions (#252).

    Iterates ``SessionService.list()`` filtered to ephemeral name prefixes
    (see :func:`is_ephemeral_session_name`) and excludes any name already
    covered by ``supervisor.plan_launches()`` so a session can never be
    classified twice.

    For each ephemeral session we ask the SessionService for raw health
    signals (``health(name)``). When the window is missing or the pane is
    dead we raise a session-scoped alert keyed by the ephemeral name —
    ``critic_failed:<task>`` for critic sessions, ``downtime_failed:<task>``
    for downtime sessions, ``ephemeral_session_dead:<task>`` for task
    workers — and skip the planned-session ``recover_session`` path
    entirely. The parent task is resolved via the work service when
    possible; when it can't be resolved, the alert falls back to keying
    on the session name itself.

    Returns a small summary suitable for the handler's job-result row.
    """
    summary = {"considered": 0, "alerts_raised": 0, "skipped_planned": 0}

    # Build the planned-session name set so we never double-classify a
    # session that already went through the supervisor sweep.
    try:
        planned_names = {
            launch.session.name for launch in supervisor.plan_launches()
        }
    except Exception:  # noqa: BLE001
        planned_names = set()

    session_service = getattr(supervisor, "session_service", None)
    if session_service is None:
        return summary

    try:
        handles = session_service.list()
    except Exception:  # noqa: BLE001
        logger.debug("ephemeral_sweep: session_service.list() failed", exc_info=True)
        return summary

    for handle in handles:
        name = getattr(handle, "name", "") or ""
        if not is_ephemeral_session_name(name):
            continue
        if name in planned_names:
            # Defensive — an ephemeral name that overlaps a planned
            # launch (shouldn't happen in production) was already
            # classified by the supervisor sweep. Skip to avoid double
            # alerts.
            summary["skipped_planned"] += 1
            continue

        summary["considered"] += 1
        try:
            health = session_service.health(name)
        except Exception:  # noqa: BLE001
            logger.debug(
                "ephemeral_sweep: health(%s) failed", name, exc_info=True,
            )
            continue

        # Mechanical failure modes — window missing or pane dead. Other
        # signals (auth failure, stuck loops, etc.) belong to the
        # planned-session classifier; for ephemerals we only act on the
        # hard failures the planner can't recover from.
        failure: tuple[str, str] | None = None
        if not getattr(health, "window_present", False):
            failure = (
                "missing_window",
                f"Ephemeral session {name} has no tmux window",
            )
        elif getattr(health, "pane_dead", False):
            failure = (
                "pane_dead",
                f"Ephemeral session {name} pane has exited",
            )

        if failure is None:
            continue

        failure_kind, failure_message = failure
        alert_type = _ephemeral_alert_type(name, failure_kind)
        try:
            store.upsert_alert(
                name,
                alert_type,
                "warn",
                # Three-question rule (#240): what / why / fix.
                f"{failure_message}. "
                f"Why it matters: parent task is blocked because the "
                f"ephemeral session that was driving it is gone. "
                f"Fix: inspect the parent task's status and re-spawn the "
                f"session via the originating handler "
                f"(critic / downtime / `pm worker-start`).",
            )
            summary["alerts_raised"] += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "ephemeral_sweep: upsert_alert failed for %s", name,
                exc_info=True,
            )
        # Deliberately do NOT call ``supervisor.maybe_recover_session`` —
        # ephemeral sessions are owned by the spawning subsystem (work
        # service for task workers, planner for critics, downtime
        # explorer for downtime sessions). Surfacing the alert is the
        # contract; the spawner decides whether to relaunch.

    return summary


def _ephemeral_alert_type(session_name: str, failure_kind: str) -> str:
    """Pick a stable, parent-task-keyed alert type for an ephemeral failure.

    Naming convention:

    * ``task-<project>-<number>`` → ``ephemeral_session_dead:<project>/<number>``
    * ``critic_<flavor>`` → ``critic_failed:<session_name>``
    * ``downtime_<slug>`` → ``downtime_failed:<session_name>``

    For ``task-*`` we attempt to extract the parent ``<project>/<number>``
    so the cockpit can correlate the alert with the task row directly. If
    the name doesn't fit the expected pattern we fall back to the raw
    session name.
    """
    if session_name.startswith("task-"):
        suffix = session_name[len("task-"):]
        # ``task-<project>-<number>`` — split from the right so projects
        # containing hyphens are handled correctly.
        if "-" in suffix:
            project, _, number = suffix.rpartition("-")
            if project and number.isdigit():
                return f"ephemeral_session_dead:{project}/{number}"
        return f"ephemeral_session_dead:{session_name}"
    if session_name.startswith("critic_"):
        return f"critic_failed:{session_name}"
    if session_name.startswith("downtime_"):
        return f"downtime_failed:{session_name}"
    return f"ephemeral_session_dead:{session_name}"


def capacity_probe_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Probe capacity for every configured account."""
    with _load_config_and_store(payload) as (config, store):
        from pollypm.capacity import probe_all_accounts

        probes = probe_all_accounts(config, store)
        summary = {probe.account_name: probe.state.value for probe in probes}
        return {"probes": summary}


def transcript_ingest_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Tail provider transcripts into the shared events ledger."""
    config = _load_config(payload)

    from pollypm.transcript_ingest import sync_transcripts_once

    sync_transcripts_once(config)
    return {"ok": True}


def work_progress_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan in_progress tasks for staleness and emit resume pings (#249).

    Complements the 10s ``session.health_sweep`` — at a lower 5-min
    cadence we iterate every ``in_progress`` task whose current actor is
    a machine (``actor_type != user``) and check:

      * The claimant session exists.
      * The claimant session is idle (``is_turn_active == False``).
      * The session hasn't recorded an event in the last 30 minutes.

    When all three hold we re-emit the assignment event through
    ``task_assignment_notify``'s ``notify()`` helper — the plugin's
    existing 30-min dedupe table (``task_notifications``) guarantees at
    most one ping per (session, task) per 30 minutes.

    Returns a small summary so the job runner records useful output.
    Never raises on a per-task failure — the sweep continues.
    """
    from datetime import UTC, datetime, timedelta

    from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
        _build_event_for_task,
    )
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        DEDUPE_WINDOW_SECONDS,
        load_runtime_services,
    )
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        notify as _notify,
    )
    from pollypm.recovery.state_reconciliation import (
        reconcile_expected_advance,
    )
    from pollypm.recovery.worker_turn_end import (
        handle_worker_turn_end,
        is_worker_session_name,
    )
    from pollypm.work.models import ActorType
    from pollypm.work.task_assignment import SessionRoleIndex

    # 30 min — mirrors the stuck_on_task threshold + dedupe window.
    STALE_THRESHOLD_SECONDS = int(
        payload.get("stale_threshold_seconds") or 1800,
    )

    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    work = services.work_service
    state_store = services.state_store
    # #349: unified Store handle for writer sites (record_event, upsert_alert,
    # clear_alert). Raw-SQL readers on ``state_store`` (``.execute``) stay
    # on the legacy StateStore until Issue F (#342) retires it.
    msg_store = services.msg_store
    session_svc = services.session_service

    if work is None:
        return {"outcome": "skipped", "reason": "no_work_service"}

    considered = 0
    pinged = 0
    skipped_active_turn = 0
    skipped_recent_event = 0
    skipped_no_session = 0
    deduped = 0
    # #296 — state-drift counters. ``drift_detected`` is the number of
    # tasks whose observable deliverables outpace their flow node on
    # this sweep; ``drift_alerted`` is the subset where we actually
    # raised a fresh alert (the rest were deduped by upsert_alert's
    # per-type uniqueness guard).
    drift_detected = 0
    drift_alerted = 0
    # #302 — worker-turn-end auto-reprompt counters. When drift fires
    # on a ``worker-*`` session we either create a blocking_question
    # inbox item (if the transcript tail shows blocker language) or
    # send the canonical reprompt via the session service.
    worker_blocking_questions = 0
    worker_reprompts = 0
    # Config is looked up once per sweep for persona_name resolution
    # on the blocking_question path. Tolerant of any failure — the
    # helper falls back to "polly" without a config.
    try:
        from pollypm.config import (
            DEFAULT_CONFIG_PATH, load_config, resolve_config_path,
        )
        _cfg_override = payload.get("config_path")
        _cfg_path = (
            Path(_cfg_override) if _cfg_override
            else resolve_config_path(DEFAULT_CONFIG_PATH)
        )
        sweep_config = load_config(_cfg_path) if _cfg_path and _cfg_path.exists() else None
    except Exception:  # noqa: BLE001
        sweep_config = None

    try:
        try:
            tasks = work.list_tasks(work_status="in_progress")
        except Exception:  # noqa: BLE001
            logger.debug(
                "work.progress_sweep: list_tasks(in_progress) failed",
                exc_info=True,
            )
            return {"outcome": "failed", "reason": "list_tasks_error"}

        # Index: resolve each event's target session handle.
        index = (
            SessionRoleIndex(session_svc, work_service=work)
            if session_svc is not None else None
        )

        now = datetime.now(UTC)
        for task in tasks:
            try:
                event = _build_event_for_task(work, task)
            except Exception:  # noqa: BLE001
                continue
            if event is None:
                continue
            # Only machine actors — we don't ping humans.
            if event.actor_type is ActorType.HUMAN:
                continue
            considered += 1

            # Resolve the target session.
            handle = None
            if index is not None:
                try:
                    handle = index.resolve(
                        event.actor_type, event.actor_name, event.project,
                    )
                except Exception:  # noqa: BLE001
                    handle = None
            if handle is None:
                skipped_no_session += 1
                continue
            target_name = getattr(handle, "name", "")
            if not target_name:
                skipped_no_session += 1
                continue

            # Skip actively-turning sessions — we don't ping mid-work.
            if session_svc is not None:
                checker = getattr(session_svc, "is_turn_active", None)
                if callable(checker):
                    try:
                        if bool(checker(target_name)):
                            skipped_active_turn += 1
                            continue
                    except Exception:  # noqa: BLE001
                        pass

            # #296 — observable flow-state drift. Check BEFORE the
            # recent-event skip: a session that just fired a ``pm
            # notify`` has a very fresh event on the ledger (the
            # notify itself), which would otherwise suppress the
            # stuck_on_task path AND mask the drift. Drift detection
            # has its own dedupe via ``upsert_alert`` — the
            # ``state_drift:<task_id>`` alert type is unique per task
            # per open alert, so repeated sweeps don't spam.
            try:
                resolver = getattr(work, "_resolve_project_path", None)
                project_path = None
                if callable(resolver):
                    try:
                        project_path = resolver(task.project)
                    except Exception:  # noqa: BLE001
                        project_path = None
                if project_path is None:
                    project_path = services.project_root
                drift = reconcile_expected_advance(
                    task,
                    Path(project_path),
                    work,
                    state_store=state_store,
                    now=now,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: drift reconcile failed for %s",
                    task.task_id, exc_info=True,
                )
                drift = None
            if drift is not None:
                drift_detected += 1
                current_node = getattr(task, "current_node_id", "") or ""
                message = (
                    f"task {task.task_id}: observed "
                    f"{drift.advance_to_node} deliverables, advancing "
                    f"from {current_node} to {drift.advance_to_node} — "
                    f"{drift.reason}"
                )
                # #349: writers prefer the unified ``messages`` Store; the
                # fallback to ``state_store`` keeps older test harnesses
                # (and the handful of callers that don't yet carry a Store)
                # working.
                audit_store = msg_store or state_store
                if audit_store is not None:
                    # Event — permanent record of the drift detection.
                    # Keyed to the session so operators can scope.
                    try:
                        if msg_store is not None:
                            msg_store.append_event(
                                scope=target_name,
                                sender=target_name,
                                subject="state_drift",
                                payload={
                                    "message": message,
                                    "task_id": task.task_id,
                                    "reason": drift.reason,
                                },
                            )
                        else:
                            state_store.record_event(
                                target_name, "state_drift", message,
                            )
                    except Exception:  # noqa: BLE001
                        pass
                    # Alert — visible warning for Polly / the cockpit.
                    # Alert type carries the task id so drift on two
                    # different tasks surfaces as two distinct alerts.
                    alert_type = f"state_drift:{task.task_id}"
                    try:
                        if msg_store is not None:
                            # Read through the Store so the "is this new?"
                            # check observes the same table the upsert
                            # writes to.
                            existing_rows = msg_store.query_messages(
                                type="alert",
                                scope=target_name,
                                sender=alert_type,
                                state="open",
                                limit=1,
                            )
                            is_new = not existing_rows
                            msg_store.upsert_alert(
                                target_name,
                                alert_type,
                                "warn",
                                (
                                    f"{target_name} drift on {task.task_id}: "
                                    f"{drift.reason}"
                                ),
                            )
                        else:
                            existing_row = state_store.execute(
                                "SELECT id FROM alerts WHERE session_name = ? "
                                "AND alert_type = ? AND status = 'open'",
                                (target_name, alert_type),
                            ).fetchone()
                            is_new = existing_row is None
                            state_store.upsert_alert(
                                target_name,
                                alert_type,
                                "warn",
                                (
                                    f"{target_name} drift on {task.task_id}: "
                                    f"{drift.reason}"
                                ),
                            )
                        if is_new:
                            drift_alerted += 1
                    except Exception:  # noqa: BLE001
                        pass

                # #302 — worker-specific auto-reprompt. For worker
                # sessions (and only worker sessions) we extend the
                # log+alert path with a concrete action: either
                # escalate the block to the project PM via an inbox
                # task or nudge the worker with the canonical reprompt.
                # Non-worker sessions (architect / reviewer / operator)
                # fall through unchanged — the alert is enough.
                if is_worker_session_name(target_name):
                    try:
                        outcome = handle_worker_turn_end(
                            task,
                            target_name,
                            work_service=work,
                            session_service=session_svc,
                            state_store=state_store,
                            config=sweep_config,
                            msg_store=msg_store,
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "work.progress_sweep: worker_turn_end "
                            "failed for %s", task.task_id, exc_info=True,
                        )
                        outcome = "skipped"
                    if outcome == "blocking_question":
                        worker_blocking_questions += 1
                    elif outcome == "reprompt":
                        worker_reprompts += 1

            # Skip sessions that have recorded ANY event recently — the
            # session is clearly still doing something; a stale task
            # here is orthogonal to session liveness.
            # #349: events moved to the unified ``messages`` table; query
            # via the Store when available, fall back to the legacy
            # ``events`` table otherwise so older callers still see their
            # own writes.
            recent_ts: str | None = None
            if msg_store is not None:
                try:
                    events = msg_store.query_messages(
                        type="event",
                        scope=target_name,
                        limit=1,
                    )
                    last_ts_stamp = events[0].get("created_at") if events else None
                    if last_ts_stamp is not None:
                        recent_ts = (
                            last_ts_stamp.isoformat()
                            if hasattr(last_ts_stamp, "isoformat")
                            else str(last_ts_stamp)
                        )
                except Exception:  # noqa: BLE001
                    pass
            if recent_ts is None and state_store is not None:
                try:
                    row = state_store.execute(
                        "SELECT created_at FROM events WHERE session_name = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (target_name,),
                    ).fetchone()
                    if row and row[0]:
                        recent_ts = str(row[0])
                except Exception:  # noqa: BLE001
                    pass
            if recent_ts is not None:
                try:
                    last_ts = datetime.fromisoformat(recent_ts)
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=UTC)
                    if (now - last_ts) < timedelta(
                        seconds=STALE_THRESHOLD_SECONDS,
                    ):
                        skipped_recent_event += 1
                        continue
                except ValueError:
                    pass

            # Fire the ping — notify() enforces the 30-min dedupe.
            try:
                outcome = _notify(
                    event,
                    services=services,
                    throttle_seconds=DEDUPE_WINDOW_SECONDS,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: notify failed for %s",
                    event.task_id, exc_info=True,
                )
                continue
            result = str(outcome.get("outcome", ""))
            if result == "deduped":
                deduped += 1
            elif result == "sent":
                pinged += 1
                # Raise a low-severity stuck_on_task alert so the
                # cockpit surfaces the event to the operator.
                # #349: writers land on the unified ``messages`` table.
                if msg_store is not None:
                    try:
                        msg_store.upsert_alert(
                            target_name,
                            f"stuck_on_task:{event.task_id}",
                            "warning",
                            (
                                f"Session {target_name} stuck on "
                                f"{event.task_id} — resume ping sent"
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        closer = getattr(work, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass

    return {
        "outcome": "swept",
        "considered": considered,
        "pinged": pinged,
        "deduped": deduped,
        "skipped_active_turn": skipped_active_turn,
        "skipped_recent_event": skipped_recent_event,
        "skipped_no_session": skipped_no_session,
        "drift_detected": drift_detected,
        "drift_alerted": drift_alerted,
        "worker_blocking_questions": worker_blocking_questions,
        "worker_reprompts": worker_reprompts,
    }


def pane_text_classify_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Semantic pane-text classifier sweep — issue #250.

    Runs the :mod:`pollypm.recovery.pane_patterns` rule set against
    every live session's captured pane text. For each rule that
    matches, raises a ``pane:<rule_name>:<session_name>`` alert via
    ``StateStore.upsert_alert`` (the alert's per-(session, type) open
    uniqueness guarantees dedupe across sweeps). Rules in
    :data:`pane_patterns.USER_VISIBLE_RULES` additionally emit a
    best-effort inbox task so Sam sees a pushable notification.

    DETECTION + ALERTS ONLY (scope constraint — Polly is live in a
    dogfood session on 2026-04-17). Any send-keys intervention (
    ``/compact``, ``Esc``, auto-accept of permission prompts, theme
    modal dismissal) is deferred to the follow-up PR and marked
    ``TODO(#250-followup)`` below.

    Clears alerts for rules that no longer match — the handler owns
    the full lifecycle so the cockpit doesn't accumulate stale
    warnings.

    Returns a small summary dict for job-result introspection.
    """
    from pollypm.plugins_builtin.task_assignment_notify.resolver import (
        load_runtime_services,
    )
    from pollypm.recovery.pane_patterns import (
        RULES,
        USER_VISIBLE_RULES,
        classify_pane,
        rule_by_name,
    )

    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)

    session_svc = services.session_service
    state_store = services.state_store
    # #349: unified Store for writer sites; StateStore stays for the raw
    # ``.execute`` paths below which target tables the Store does not own.
    msg_store = services.msg_store
    work_service = services.work_service

    if session_svc is None or state_store is None:
        return {"outcome": "skipped", "reason": "services_unavailable"}

    capture_lines = int(payload.get("capture_lines", 200) or 200)

    # Pre-resolve the full rule-name set so we can clear alerts for
    # rules that no longer match on this session.
    all_rule_names = [rule.name for rule in RULES]

    sessions_scanned = 0
    alerts_raised = 0
    alerts_cleared = 0
    inbox_items_emitted = 0
    capture_failures = 0
    match_counts: dict[str, int] = {name: 0 for name in all_rule_names}

    try:
        handles = session_svc.list()
    except Exception:  # noqa: BLE001
        logger.debug("pane_text_classify: session list failed", exc_info=True)
        return {"outcome": "failed", "reason": "session_list_error"}

    for handle in handles:
        session_name = getattr(handle, "name", "") or ""
        if not session_name:
            continue
        sessions_scanned += 1

        capture_fn = getattr(session_svc, "capture", None)
        if not callable(capture_fn):
            capture_failures += 1
            continue
        try:
            pane_text = capture_fn(session_name, lines=capture_lines)
        except Exception:  # noqa: BLE001
            logger.debug(
                "pane_text_classify: capture failed for %s",
                session_name, exc_info=True,
            )
            capture_failures += 1
            continue
        if not isinstance(pane_text, str):
            pane_text = ""

        try:
            matched = set(classify_pane(pane_text))
        except Exception:  # noqa: BLE001
            logger.debug(
                "pane_text_classify: classify failed for %s",
                session_name, exc_info=True,
            )
            continue

        for rule_name in all_rule_names:
            alert_type = f"pane:{rule_name}"
            if rule_name in matched:
                rule = rule_by_name(rule_name)
                severity = rule.severity if rule else "warn"
                message = (
                    f"{session_name}: pane-text pattern "
                    f"'{rule_name}' matched"
                )
                try:
                    # Detect first-fire so the counter reflects real
                    # new notifications rather than idempotent upserts.
                    # #349: read + write through the unified Store.
                    if msg_store is not None:
                        existing = msg_store.query_messages(
                            type="alert",
                            scope=session_name,
                            sender=alert_type,
                            state="open",
                            limit=1,
                        )
                        msg_store.upsert_alert(
                            session_name, alert_type, severity, message,
                        )
                        is_new = not existing
                    else:  # pragma: no cover — Store fallback path.
                        existing_row = state_store.execute(
                            "SELECT id FROM alerts WHERE session_name = ? "
                            "AND alert_type = ? AND status = 'open'",
                            (session_name, alert_type),
                        ).fetchone()
                        state_store.upsert_alert(
                            session_name, alert_type, severity, message,
                        )
                        is_new = existing_row is None
                    if is_new:
                        alerts_raised += 1
                        match_counts[rule_name] += 1
                        # Ledger event so the activity feed carries a
                        # permanent record of the detection regardless
                        # of how long the alert stays open.
                        try:
                            if msg_store is not None:
                                msg_store.append_event(
                                    scope=session_name,
                                    sender=session_name,
                                    subject="pane.classify.match",
                                    payload={
                                        "message": (
                                            f"matched rule '{rule_name}'"
                                        ),
                                        "rule": rule_name,
                                    },
                                )
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "pane_text_classify: upsert_alert failed "
                        "for %s/%s", session_name, rule_name,
                        exc_info=True,
                    )

                # TODO(#250-followup): wire send-keys intervention
                # after Sam reviews. For context_full → send '/compact';
                # for permission_prompt → optional auto-accept via
                # `pm send <session> 1`; for theme_trust_modal →
                # dismiss via Enter. All deferred — Polly is live in a
                # dogfood session and any stray send_keys would clobber
                # her turn.

                if rule_name in USER_VISIBLE_RULES and work_service is not None:
                    emitted = _emit_pane_pattern_inbox_item(
                        work_service=work_service,
                        session_name=session_name,
                        rule_name=rule_name,
                        pane_text=pane_text,
                        state_store=state_store,
                        msg_store=msg_store,
                    )
                    if emitted:
                        inbox_items_emitted += 1
            else:
                # Rule doesn't match anymore — clear any open alert.
                # ``clear_alert`` is a no-op when no open alert exists.
                # #349: read + clear via the unified Store.
                try:
                    if msg_store is not None:
                        existing = msg_store.query_messages(
                            type="alert",
                            scope=session_name,
                            sender=alert_type,
                            state="open",
                            limit=1,
                        )
                        if existing:
                            msg_store.clear_alert(session_name, alert_type)
                            alerts_cleared += 1
                    else:  # pragma: no cover — Store fallback path.
                        existing_row = state_store.execute(
                            "SELECT id FROM alerts WHERE session_name = ? "
                            "AND alert_type = ? AND status = 'open'",
                            (session_name, alert_type),
                        ).fetchone()
                        if existing_row is not None:
                            state_store.clear_alert(session_name, alert_type)
                            alerts_cleared += 1
                except Exception:  # noqa: BLE001
                    pass

    return {
        "outcome": "swept",
        "sessions_scanned": sessions_scanned,
        "alerts_raised": alerts_raised,
        "alerts_cleared": alerts_cleared,
        "inbox_items_emitted": inbox_items_emitted,
        "capture_failures": capture_failures,
        "match_counts": match_counts,
    }


def _emit_pane_pattern_inbox_item(
    *,
    work_service: Any,
    session_name: str,
    rule_name: str,
    pane_text: str,
    state_store: Any = None,
    msg_store: Any = None,
) -> bool:
    """Create a user-visible inbox task for a matched pane pattern.

    Dedupes via a stable ``labels`` tag — callers scan by label before
    creating. Returns ``True`` when a new inbox task was created,
    ``False`` when one already existed or creation failed (best-effort
    so a flaky work service never crashes the sweep).
    """
    dedupe_label = f"pane_pattern:{rule_name}:{session_name}"

    # Best-effort dedupe: scan the inbox project's open/in_progress
    # tasks for one carrying our sidecar label. The work_service API
    # doesn't filter on labels server-side, so we filter in Python —
    # acceptable because the inbox project's open task count is small
    # (single-digit at steady state).
    try:
        list_fn = getattr(work_service, "list_tasks", None)
        if callable(list_fn):
            for status in ("queued", "in_progress", "draft", "review"):
                try:
                    tasks = list_fn(work_status=status, project="inbox")
                except TypeError:
                    # Older fakes may not accept ``project`` — fall
                    # back to status-only.
                    tasks = list_fn(work_status=status)
                for task in tasks or []:
                    labels = getattr(task, "labels", None) or []
                    if dedupe_label in labels:
                        return False
    except Exception:  # noqa: BLE001
        # A list failure isn't fatal — worst case we duplicate, which
        # is visible + fixable rather than silent drops.
        pass

    title_map = {
        "context_full": (
            f"Session '{session_name}' approaching context limit — "
            f"consider /compact"
        ),
        "permission_prompt": (
            f"Session '{session_name}' is waiting on a permission "
            f"prompt — approval needed"
        ),
    }
    title = title_map.get(
        rule_name,
        f"Session '{session_name}' matched pane pattern '{rule_name}'",
    )

    # Short excerpt for the inbox body — keep tight so the UI list
    # stays readable. We take the tail of the capture because the
    # relevant prompt / warning is almost always the most recent thing
    # rendered.
    excerpt_source = pane_text[-600:] if pane_text else ""
    body_parts = [
        f"Session **{session_name}** matched pane-text rule "
        f"**{rule_name}**.",
        "",
        "## Recent pane text",
        "",
        "```",
        excerpt_source.strip() or "(empty capture)",
        "```",
        "",
        "## How to resolve",
        "",
    ]
    if rule_name == "context_full":
        body_parts.extend([
            f"- Attach (`tmux attach -t {session_name}`) and run "
            "`/compact` to summarize, or",
            f"- Send from the cockpit: `pm send {session_name} /compact`.",
        ])
    elif rule_name == "permission_prompt":
        body_parts.extend([
            f"- Attach (`tmux attach -t {session_name}`) and approve "
            "the prompt, or",
            f"- Auto-accept: `pm send {session_name} 1`.",
        ])
    body_parts.extend([
        "",
        f"Alert type: `pane:{rule_name}`. This inbox item was emitted "
        "by the pane-text classifier (issue #250).",
    ])
    body = "\n".join(body_parts)

    labels = [
        "pane_pattern",
        f"rule:{rule_name}",
        f"session:{session_name}",
        dedupe_label,
    ]

    try:
        inbox_task = work_service.create(
            title=title,
            description=body,
            type="task",
            project="inbox",
            flow_template="chat",
            roles={"requester": session_name, "operator": "polly"},
            priority="normal",
            created_by=session_name,
            labels=labels,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "pane_text_classify: inbox create failed for %s/%s",
            session_name, rule_name, exc_info=True,
        )
        return False

    # #349: audit event lands on the unified ``messages`` table via the
    # Store. Falls back to the legacy StateStore when no Store is wired
    # through (older call paths / tests).
    if msg_store is not None:
        task_id = getattr(inbox_task, "task_id", "") or ""
        try:
            msg_store.append_event(
                scope=session_name,
                sender=session_name,
                subject="pane.classify.inbox_emitted",
                payload={
                    "message": (
                        f"emitted inbox task {task_id} for "
                        f"rule '{rule_name}'"
                    ),
                    "task_id": task_id,
                    "rule": rule_name,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return True


def alerts_gc_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Release expired leases and prune old heartbeat rows.

    Leases are auto-released via ``Supervisor.release_expired_leases``;
    heartbeat rows older than 24h are pruned via
    ``StateStore.prune_old_data``. Both are cheap, idempotent, and safe
    to run from any worker thread.

    Events pruning was previously done here with a blanket 7-day cutoff,
    which contradicted the tiered ``[events]`` retention policy
    (``events.retention_sweep`` handler). Events are now exclusively
    managed by that policy — audit events keep their 365-day floor and
    high-volume noise still gets swept at 7 days, but explicitly via
    tier rather than blunt cutoff.
    """
    with _load_config_and_store(payload) as (config, store):
        # Lease GC lives on the supervisor because it records events with
        # owner context; use a transient supervisor here.
        from pollypm.supervisor import Supervisor

        supervisor = Supervisor(config)
        released = supervisor.release_expired_leases()

        # event_days=10**6 effectively no-ops events pruning while keeping
        # the heartbeat cutoff at the existing 24h. The events.retention_sweep
        # handler is now authoritative for the events table.
        pruned = store.prune_old_data(event_days=10**6)
        return {
            "leases_released": len(released),
            "events_pruned": int(pruned.get("events", 0)),
            "heartbeats_pruned": int(pruned.get("heartbeats", 0)),
        }


def db_vacuum_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Run an incremental vacuum against StateStore to reclaim freelist pages.

    Incremental vacuum is a low-cost operation that only touches pages on
    the SQLite freelist — it does not rewrite the whole database the way
    a full ``VACUUM`` would. Safe to call daily. The shared StateStore
    connection coordinates with concurrent writers via busy_timeout, so
    no external lock is needed beyond what StateStore already holds.
    """
    with _load_config_and_store(payload) as (_config, store):
        bytes_reclaimed = store.incremental_vacuum()
        mb_reclaimed = bytes_reclaimed / (1024 * 1024)
        # #349: audit lands on ``messages`` via the unified Store.
        msg_store = _open_msg_store(_config)
        try:
            if msg_store is not None:
                msg_store.append_event(
                    scope="system",
                    sender="system",
                    subject="db.vacuum",
                    payload={
                        "message": f"reclaimed {mb_reclaimed:.1f}MB",
                        "bytes_reclaimed": bytes_reclaimed,
                    },
                )
        finally:
            _close_msg_store(msg_store)
        return {"bytes_reclaimed": bytes_reclaimed, "mb_reclaimed": mb_reclaimed}


def events_retention_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply tiered retention to the ``events`` table (issue-tracked as #267 follow-up).

    The events ledger is written to on every heartbeat, token-ledger
    bump, and task transition — unbounded growth is the single biggest
    contributor to ``state.db`` bloat on long-running installations.
    This handler walks four tiers (``audit`` / ``operational`` /
    ``high_volume`` / ``default``) and DELETEs rows older than each
    tier's retention window. One parameterized DELETE per tier — no
    row-by-row work.

    Runs hourly (``37 * * * *`` — off-pattern from the 4am DB hygiene
    window) so the freelist stays small; the daily ``db.vacuum``
    ``PRAGMA incremental_vacuum`` then reclaims the pages. No explicit
    code dependency between the two — purely a cadence contract.

    Tier membership is data, defined in
    ``pollypm.storage.events_retention``. Retention *windows* are
    configurable via the ``[events]`` TOML section; the handler honours
    whatever ``config.events`` resolves to.

    Emits a single ``events.retention_sweep`` event only when rows were
    deleted — a no-op sweep stays silent to avoid the handler logging
    its own cadence and defeating the purpose.
    """
    from pollypm.storage.events_retention import (
        RetentionPolicy,
        sweep_events,
    )

    with _load_config_and_store(payload) as (config, store):
        settings = config.events
        policy = RetentionPolicy(
            audit_days=settings.audit_retention_days,
            operational_days=settings.operational_retention_days,
            high_volume_days=settings.high_volume_retention_days,
            default_days=settings.default_retention_days,
        )

        # Serialize against the StateStore's own lock so we don't race
        # with writers on the shared connection. ``sweep_events`` issues
        # a commit, so the rowcounts are durable before we return.
        with store._lock:  # noqa: SLF001 — StateStore exposes no public wrapper
            result = sweep_events(store._conn, policy)  # noqa: SLF001

        counts = {
            "deleted_audit": result.deleted_audit,
            "deleted_operational": result.deleted_operational,
            "deleted_high_volume": result.deleted_high_volume,
            "deleted_default": result.deleted_default,
            "total": result.total,
        }

        # Only log when something actually happened — otherwise every
        # hourly sweep would itself become a ``high_volume`` event and
        # grow the table we're trying to shrink.
        # #349: audit lands on ``messages`` via the unified Store.
        if result.total > 0:
            msg_store = _open_msg_store(config)
            try:
                if msg_store is not None:
                    msg_store.append_event(
                        scope="system",
                        sender="system",
                        subject="events.retention_sweep",
                        payload={
                            "message": (
                                f"deleted {result.total} events "
                                f"(audit={result.deleted_audit}, "
                                f"operational={result.deleted_operational}, "
                                f"high_volume={result.deleted_high_volume}, "
                                f"default={result.deleted_default})"
                            ),
                            **counts,
                        },
                    )
            finally:
                _close_msg_store(msg_store)

        return counts


def memory_ttl_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop expired memory_entries (TTL in the past).

    Only affects rows with ``ttl_at IS NOT NULL``. Rows without an
    explicit TTL are left alone — retention policy for those is a
    separate decision, this handler just enforces what's already on
    the row.
    """
    with _load_config_and_store(payload) as (_config, store):
        deleted = store.sweep_expired_memory_entries()
        # #349: audit lands on ``messages`` via the unified Store.
        msg_store = _open_msg_store(_config)
        try:
            if msg_store is not None:
                msg_store.append_event(
                    scope="system",
                    sender="system",
                    subject="memory.ttl_sweep",
                    payload={
                        "message": f"dropped {deleted} expired entries",
                        "deleted": deleted,
                    },
                )
        finally:
            _close_msg_store(msg_store)
        return {"deleted": deleted}


def agent_worktree_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Prune stale Claude Code harness agent worktrees under ``.claude/worktrees/``.

    These are NOT PollyPM task worktrees (those live under ``<project>/.pollypm/
    worktrees/...`` and are owned by ``teardown_worker``). They are harness
    worktrees spawned by background ``Agent()`` calls with
    ``isolation: "worktree"``. The harness doesn't always clean up after
    itself — on Sam's machine the directory bloated to 6.6 GB across 59
    worktrees. This handler performs conservative cleanup:

    * Merged-to-main branches: prune via ``git worktree remove --force`` and
      drop the local branch.
    * Unmerged + mtime > 7 days: log a warning but do not delete (may be
      in-progress uncommitted work).
    * mtime < 1 hour: skip (still actively in use).

    Only directories matching ``<repo_root>/.claude/worktrees/agent-*`` are
    considered. The repo root is taken from ``payload['project_root']`` if
    provided, otherwise from config's ``project.root_dir``.
    """
    import subprocess
    import time

    hint = payload.get("project_root") if isinstance(payload, dict) else None
    if hint:
        repo_root = Path(hint)
    else:
        config = _load_config(payload)
        repo_root = config.project.root_dir

    worktrees_dir = repo_root / ".claude" / "worktrees"
    if not worktrees_dir.is_dir():
        return {"pruned": 0, "skipped_active": 0, "warned_stale": 0, "errors": 0}

    now = time.time()
    one_hour = 3600.0
    seven_days = 7 * 86400.0

    def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, check=False,
        )

    # Pre-compute the merged-branch set once via two cheap calls — cheaper
    # than spawning two git processes per worktree.
    merged_local = _git(repo_root, "branch", "--merged", "main")
    merged_remote = _git(repo_root, "branch", "-r", "--merged", "origin/main")
    merged_names: set[str] = set()
    for proc in (merged_local, merged_remote):
        if proc.returncode != 0:
            continue
        for raw in proc.stdout.splitlines():
            # ``git branch`` prefixes lines with ``* `` (current),
            # ``+ `` (checked out in another worktree), or spaces.
            name = raw.strip().lstrip("*+").strip()
            if not name or name.startswith("("):
                continue
            # Normalize ``origin/foo`` → ``foo`` so local+remote merge into
            # one name set.
            if name.startswith("origin/"):
                name = name[len("origin/"):]
            merged_names.add(name)

    pruned = 0
    skipped_active = 0
    warned_stale = 0
    errors = 0

    for wt in sorted(worktrees_dir.glob("agent-*")):
        if not wt.is_dir():
            continue
        try:
            mtime = wt.stat().st_mtime
            age = now - mtime
            if age < one_hour:
                skipped_active += 1
                continue

            branch_proc = _git(wt, "branch", "--show-current")
            if branch_proc.returncode != 0:
                errors += 1
                continue
            branch = branch_proc.stdout.strip()
            if not branch:
                errors += 1
                continue

            if branch in merged_names:
                remove_proc = _git(
                    repo_root, "worktree", "remove", "--force", str(wt),
                )
                if remove_proc.returncode != 0:
                    errors += 1
                    continue
                # Best-effort local branch delete — don't fail the prune
                # if the branch was already gone.
                _git(repo_root, "branch", "-D", branch)
                pruned += 1
            elif age > seven_days:
                logger.warning(
                    "agent_worktree.prune: stale unmerged worktree %s "
                    "(branch=%s, age_days=%.1f) — leaving in place",
                    wt, branch, age / 86400.0,
                )
                warned_stale += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "agent_worktree.prune: error processing %s", wt, exc_info=True,
            )
            errors += 1

    # Clean up any dangling worktree admin entries (e.g. directories that
    # were removed on disk but still registered in ``.git/worktrees``).
    _git(repo_root, "worktree", "prune")

    return {
        "pruned": pruned,
        "skipped_active": skipped_active,
        "warned_stale": warned_stale,
        "errors": errors,
    }


def worktree_state_audit_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify every active worker-session worktree + surface blockers (#251).

    Complements the hourly ``agent_worktree.prune`` handler (which GCs
    orphans) with a 10-minute *state* sweep over the
    ``work_sessions`` rows the session-manager owns. For each row with
    a ``worktree_path`` we classify the worktree via
    ``pollypm.worktree_audit.classify_worktree_state`` and act:

    * ``merge_conflict`` → open ``worktree_state:<task>:merge_conflict``
      alert (severity ``error``) AND, if the task is claimed, emit a
      blocking inbox task via the same work-service path ``pm notify``
      uses so the user sees it immediately.
    * ``lock_file`` → alert ``worktree_state:<task>:lock_file``. Age
      <5min → ``warn``; ≥5min → ``error`` (escalated).
    * ``detached_head`` → alert ``worktree_state:<task>:detached_head``
      (``warn``). No inbox — the heartbeat reprompt path handles
      recovery.
    * ``dirty_expected`` → tracked but silent *unless* the worktree's
      mtime is >60 min old, in which case a low-severity
      ``worktree_state:<task>:dirty_stale`` alert fires. The
      staleness test uses the directory mtime because git operations
      bump it — we stay silent while the worker is still typing.
    * ``orphan_branch`` → inbox nudge (``routine`` priority) once per
      task per sweep-dedup window. Alert also raised for cockpit
      visibility.
    * ``clean`` → any open ``worktree_state:<task>:*`` alerts are
      cleared.

    All alerts are keyed by ``(session_name, worktree_state:<task>:<st>)``
    so the ``upsert_alert`` uniqueness guard auto-dedupes repeat
    observations and ``clear_alert`` can resolve them when the state
    flips back to ``clean``.

    Returns a summary of counts for the job-result ledger — no per-
    worktree detail to keep the event row small.
    """
    import time as _time

    from pollypm.worktree_audit import (
        WorktreeState,
        classify_worktree_state,
    )

    with _load_config_and_store(payload) as (config, store):
        # #349: writers flip onto the unified ``messages`` table via Store.
        # ``store`` (StateStore) stays for raw ``.execute`` reads on the
        # legacy ``alerts`` table below — those reads run in parallel with
        # the Store-backed writes so the alert-exists check + the new
        # upsert still observe the same table during the rollout.
        # TODO(#342-F): once StateStore is retired, flip the read to
        # ``msg_store.query_messages(type='alert', scope=..., sender=...)``.
        msg_store = _open_msg_store(config)

        # Work service — required to list active work_sessions. Open via
        # the same path ``work.progress_sweep`` uses so we honour the
        # workspace-root DB convention.
        try:
            from pollypm.work.sqlite_service import SQLiteWorkService

            project_root = config.project.root_dir
            db_path = project_root / ".pollypm" / "state.db"
            work = SQLiteWorkService(db_path=db_path, project_path=project_root)
        except Exception:  # noqa: BLE001
            logger.debug(
                "worktree.state_audit: work service unavailable", exc_info=True,
            )
            _close_msg_store(msg_store)
            return {"outcome": "skipped", "reason": "no_work_service"}

        # All alert types this handler owns — used to clear stale alerts
        # when we transition into ``CLEAN``. Keep in sync with the
        # branches below.
        STATE_ALERT_TYPES: tuple[str, ...] = (
            "merge_conflict", "lock_file", "detached_head",
            "dirty_stale", "orphan_branch",
        )

        considered = 0
        classified: dict[str, int] = {}
        alerts_raised = 0
        alerts_cleared = 0
        inbox_emitted = 0

        # Thresholds — kept local (no config knob yet) so they're explicit
        # in the handler body for reviewers.
        LOCK_ESCALATE_SECONDS = 5 * 60       # lock >5min → severity error
        DIRTY_STALE_SECONDS = 60 * 60        # dirty + mtime > 60min → alert

        now_epoch = _time.time()

        try:
            try:
                sessions = work.list_worker_sessions(active_only=True)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "worktree.state_audit: list_worker_sessions failed",
                    exc_info=True,
                )
                return {"outcome": "failed", "reason": "list_sessions_error"}

            for sess in sessions:
                wt_path_raw = getattr(sess, "worktree_path", None)
                if not wt_path_raw:
                    continue
                considered += 1
                wt_path = Path(wt_path_raw)
                task_id = f"{sess.task_project}/{sess.task_number}"
                agent = sess.agent_name or "worker"
                # The alert namespace is the agent session name so the
                # cockpit's per-session view shows the blocker alongside
                # other heartbeat-raised alerts.
                session_key = agent

                try:
                    classification = classify_worktree_state(wt_path)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "worktree.state_audit: classify failed for %s",
                        wt_path, exc_info=True,
                    )
                    continue
                state = classification.state
                classified[state.value] = classified.get(state.value, 0) + 1

                # Resolve the all-clear path for CLEAN / MISSING so any
                # prior alerts auto-close.
                if state in (WorktreeState.CLEAN, WorktreeState.MISSING):
                    for kind in STATE_ALERT_TYPES:
                        alert_type = f"worktree_state:{task_id}:{kind}"
                        try:
                            existing = store.execute(
                                "SELECT id FROM alerts WHERE session_name = ? "
                                "AND alert_type = ? AND status = 'open'",
                                (session_key, alert_type),
                            ).fetchone()
                        except Exception:  # noqa: BLE001
                            existing = None
                        if existing is None:
                            continue
                        try:
                            (msg_store or store).clear_alert(session_key, alert_type)
                            alerts_cleared += 1
                        except Exception:  # noqa: BLE001
                            pass
                    continue

                # --- non-clean branches ---
                if state is WorktreeState.MERGE_CONFLICT:
                    alert_type = f"worktree_state:{task_id}:merge_conflict"
                    files = classification.metadata.get("conflict_files", [])
                    file_blurb = (
                        f" ({len(files)} file{'s' if len(files) != 1 else ''})"
                        if files else ""
                    )
                    message = (
                        f"{agent}: merge conflict in {wt_path}{file_blurb} on task "
                        f"{task_id}. Worker is blocked until the conflict resolves."
                    )
                    _raise_alert(msg_store or store, session_key,alert_type, "error", message)
                    alerts_raised += 1
                    # Inbox — the user needs to either resolve or reassign.
                    fix_hint = (
                        f"Run `git -C {wt_path} status` to inspect the conflict, "
                        f"then resolve and `git commit` or reassign the task."
                    )
                    body = (
                        f"Worker {agent} hit a merge conflict in {wt_path} while "
                        f"working on task {task_id}.\n\n"
                        f"{len(files)} conflicted file(s) detected.\n\n"
                        f"Fix: {fix_hint}"
                    )
                    if _emit_inbox_task(
                        work,
                        subject=f"Merge conflict: {task_id}",
                        body=body,
                        actor=agent,
                        dedupe_label=f"worktree_audit:{task_id}:merge_conflict",
                        project=sess.task_project,
                    ):
                        inbox_emitted += 1

                elif state is WorktreeState.LOCK_FILE:
                    lock_age = float(
                        classification.metadata.get("lock_age_seconds", 0.0),
                    )
                    severity = "error" if lock_age >= LOCK_ESCALATE_SECONDS else "warn"
                    minutes = max(1, int(lock_age // 60))
                    alert_type = f"worktree_state:{task_id}:lock_file"
                    lock_path = classification.metadata.get("lock_path", "")
                    message = (
                        f"{agent}: git lock held on {wt_path} for ~{minutes}min "
                        f"(task {task_id}). If no git process is running, remove "
                        f"{lock_path or '<gitdir>/index.lock'}."
                    )
                    _raise_alert(msg_store or store, session_key,alert_type, severity, message)
                    alerts_raised += 1

                elif state is WorktreeState.DETACHED_HEAD:
                    alert_type = f"worktree_state:{task_id}:detached_head"
                    sha = classification.metadata.get("head_sha", "")
                    message = (
                        f"{agent}: worktree {wt_path} on detached HEAD "
                        f"{sha or '(unknown)'} (task {task_id}). "
                        f"Fix: checkout the task branch before the worker "
                        f"can push."
                    )
                    _raise_alert(msg_store or store, session_key,alert_type, "warn", message)
                    alerts_raised += 1

                elif state is WorktreeState.DIRTY_EXPECTED:
                    try:
                        mtime = wt_path.stat().st_mtime
                    except OSError:
                        mtime = now_epoch
                    age_s = now_epoch - mtime
                    alert_type = f"worktree_state:{task_id}:dirty_stale"
                    if age_s >= DIRTY_STALE_SECONDS:
                        message = (
                            f"{agent}: {wt_path} has uncommitted changes and "
                            f"hasn't been touched in ~{int(age_s // 60)}min "
                            f"(task {task_id}). Fix: check in on the worker — "
                            f"likely stuck or idle."
                        )
                        _raise_alert(msg_store or store, session_key,alert_type, "warn", message)
                        alerts_raised += 1
                    else:
                        # Fresh dirt — clear any prior stale alert.
                        try:
                            existing = store.execute(
                                "SELECT id FROM alerts WHERE session_name = ? "
                                "AND alert_type = ? AND status = 'open'",
                                (session_key, alert_type),
                            ).fetchone()
                        except Exception:  # noqa: BLE001
                            existing = None
                        if existing is not None:
                            try:
                                (msg_store or store).clear_alert(session_key, alert_type)
                                alerts_cleared += 1
                            except Exception:  # noqa: BLE001
                                pass

                elif state is WorktreeState.ORPHAN_BRANCH:
                    age_days = float(classification.metadata.get("age_days", 0.0))
                    alert_type = f"worktree_state:{task_id}:orphan_branch"
                    message = (
                        f"{agent}: {wt_path} on local-only branch "
                        f"{classification.branch or '(unknown)'} with no upstream "
                        f"and no commit in ~{age_days:.1f}d (task {task_id}). "
                        f"Fix: push or archive the branch before the prune "
                        f"handler GCs it."
                    )
                    _raise_alert(msg_store or store, session_key,alert_type, "info", message)
                    alerts_raised += 1
                    # Low-severity inbox nudge.
                    body = (
                        f"Worker {agent}'s worktree for task {task_id} is on a "
                        f"local-only branch with no upstream and ~{age_days:.1f} "
                        f"days of inactivity.\n\n"
                        f"Path: {wt_path}\n\n"
                        f"Fix: push the branch, merge/abandon the task, or let "
                        f"the hourly `agent_worktree.prune` handler decide."
                    )
                    if _emit_inbox_task(
                        work,
                        subject=f"Orphan worktree branch: {task_id}",
                        body=body,
                        actor=agent,
                        dedupe_label=f"worktree_audit:{task_id}:orphan_branch",
                        project=sess.task_project,
                    ):
                        inbox_emitted += 1
        finally:
            closer = getattr(work, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
            _close_msg_store(msg_store)

        return {
            "outcome": "swept",
            "considered": considered,
            "classified": classified,
            "alerts_raised": alerts_raised,
            "alerts_cleared": alerts_cleared,
            "inbox_emitted": inbox_emitted,
        }


def _raise_alert(
    store: Any, session_name: str, alert_type: str, severity: str, message: str,
) -> None:
    """Thin wrapper that swallows a failing alert write.

    Matches the ``try/except Exception`` pattern used by ``work.
    progress_sweep`` — a single alert failure must not abort the rest
    of the sweep.
    """
    try:
        store.upsert_alert(session_name, alert_type, severity, message)
    except Exception:  # noqa: BLE001
        logger.debug(
            "worktree.state_audit: upsert_alert failed for %s/%s",
            session_name, alert_type, exc_info=True,
        )


def _emit_inbox_task(
    work: Any,
    *,
    subject: str,
    body: str,
    actor: str,
    dedupe_label: str,
    project: str,
) -> bool:
    """Create a user-routed inbox task on the chat flow.

    Mirrors the ``pm notify`` immediate-tier path in ``cli.py`` — a
    work-service task on the ``chat`` flow with ``roles.requester=user``
    is what the inbox_view filter picks up. We attach
    ``audit:worktree_state`` + the dedupe label so repeated sweeps of
    the same blocker don't mint duplicate inbox items (the label-based
    dedupe is done by the list-tasks query below).

    Returns True when a fresh task was created, False on skip (dedup
    or failure).
    """
    try:
        # Dedupe: if any task on this project already carries our
        # ``audit:<label>`` label and is still open, skip. The label
        # namespace is distinct enough that a second ``startswith``
        # scan stays cheap.
        existing = work.list_tasks(project=project, work_status="queued")
        existing += work.list_tasks(project=project, work_status="in_progress")
        for task in existing:
            labels = getattr(task, "labels", None) or ()
            if dedupe_label in labels:
                return False
    except Exception:  # noqa: BLE001
        # Listing failed — proceed to create. A duplicate is better
        # than a missing blocker notification.
        pass

    try:
        work.create(
            title=subject,
            description=body,
            type="task",
            project=project,
            flow_template="chat",
            labels=[
                "audit:worktree_state",
                dedupe_label,
            ],
            roles={"requester": "user", "actor": actor},
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "worktree.state_audit: create inbox task failed for %s",
            dedupe_label, exc_info=True,
        )
        return False


def log_rotate_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Rotate + prune oversized log files under ``config.project.logs_dir``.

    Unbounded tmux ``pipe-pane`` captures previously let individual logs
    grow to tens of megabytes (``pm-operator.log`` hit 60 MB on Sam's
    machine, total ``~/.pollypm/logs/`` reached 745 MB). This handler
    implements rename-then-truncate rotation + gzip of the rotated
    archive + retention pruning of older ``.log.<ts>.gz`` siblings.

    Algorithm per ``<logs_dir>/*.log`` file:

    1. If size <= ``rotate_size_mb`` MB → skip.
    2. Else: rename ``<name>.log`` → ``<name>.log.<ts>`` (atomic on
       POSIX). Active writers keep their open file descriptor pointed
       at the renamed inode — they do not follow the rename — so we
       recreate an empty ``<name>.log`` so new appends (including
       ``tmux pipe-pane`` when it reopens) have somewhere to go. The
       original writers will continue writing to the now-renamed file
       until they close/reopen; we accept that small tail because tmux
       reopens on its own schedule.
    3. Gzip-in-place: ``<name>.log.<ts>`` → ``<name>.log.<ts>.gz``.
    4. Retention: keep only the ``rotate_keep`` most recent ``.log.*.gz``
       siblings per base name; delete older rotations.

    Non-``.log`` files in the directory (e.g. JSON state blobs) are
    never touched. A missing ``logs_dir`` is a no-op — returns zeros
    with no error.

    Payload overrides (for tests + ad-hoc runs):
    * ``logs_dir`` — override ``config.project.logs_dir``.
    * ``rotate_size_mb`` — override ``config.logging.rotate_size_mb``.
    * ``rotate_keep`` — override ``config.logging.rotate_keep``.
    """
    import gzip
    import os
    import re
    import shutil
    import time

    # Resolve logs_dir + thresholds. When a ``logs_dir`` override is
    # present in the payload we skip loading config entirely so tests
    # don't need a full PollyPM config on disk.
    logs_dir_hint = payload.get("logs_dir") if isinstance(payload, dict) else None
    size_override = payload.get("rotate_size_mb") if isinstance(payload, dict) else None
    keep_override = payload.get("rotate_keep") if isinstance(payload, dict) else None

    if logs_dir_hint is not None:
        logs_dir = Path(logs_dir_hint)
        rotate_size_mb = int(size_override) if size_override is not None else 20
        rotate_keep = int(keep_override) if keep_override is not None else 3
    else:
        config = _load_config(payload)
        logs_dir = config.project.logs_dir
        rotate_size_mb = (
            int(size_override) if size_override is not None
            else config.logging.rotate_size_mb
        )
        rotate_keep = (
            int(keep_override) if keep_override is not None
            else config.logging.rotate_keep
        )

    if not logs_dir.is_dir():
        return {"rotated": 0, "deleted": 0, "errors": 0}

    threshold_bytes = max(1, rotate_size_mb) * 1024 * 1024
    rotated = 0
    deleted = 0
    errors = 0

    # Fixed ts-suffix pattern: <base>.log.<digits>.gz — we use epoch
    # seconds so retention ordering is a simple numeric sort.
    rotation_re = re.compile(r"^(?P<base>.+)\.log\.(?P<ts>\d+)\.gz$")

    for log_path in sorted(logs_dir.glob("*.log")):
        if not log_path.is_file():
            continue
        try:
            size = log_path.stat().st_size
        except OSError:
            errors += 1
            continue
        if size <= threshold_bytes:
            continue
        # Rotate. Use epoch seconds for the stamp — it sorts numerically
        # and avoids the filesystem-safety concerns of ISO strings.
        ts = int(time.time())
        rotated_path = log_path.with_suffix(f".log.{ts}")
        # If the rotated name already exists (two rotations in the same
        # second), bump until free.
        bump = 0
        while rotated_path.exists():
            bump += 1
            rotated_path = log_path.with_suffix(f".log.{ts}.{bump}")
        try:
            # Atomic rename on POSIX. Writers with the file open keep
            # their fd; new opens see the fresh empty file we create
            # next.
            os.rename(log_path, rotated_path)
            # Recreate an empty <name>.log so the next writer to open()
            # finds it. ``touch`` semantics.
            log_path.touch()
        except OSError:
            logger.debug(
                "log.rotate: rename failed for %s", log_path, exc_info=True,
            )
            errors += 1
            continue
        # Gzip the rotated file in place. Stream to avoid loading the
        # whole thing into memory.
        gz_path = rotated_path.with_suffix(rotated_path.suffix + ".gz")
        try:
            with open(rotated_path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            rotated_path.unlink()
            rotated += 1
        except OSError:
            logger.debug(
                "log.rotate: gzip failed for %s", rotated_path, exc_info=True,
            )
            errors += 1
            # Leave the uncompressed rotation in place so it's not lost.
            continue

    # Retention pass: group ``<base>.log.<ts>.gz`` files by base name
    # and delete all but the newest ``rotate_keep``.
    by_base: dict[str, list[tuple[int, Path]]] = {}
    for gz in logs_dir.glob("*.log.*.gz"):
        m = rotation_re.match(gz.name)
        if not m:
            continue
        try:
            ts_val = int(m.group("ts"))
        except ValueError:
            continue
        by_base.setdefault(m.group("base"), []).append((ts_val, gz))

    for base, entries in by_base.items():
        entries.sort(key=lambda item: item[0], reverse=True)
        for _ts_val, gz_path in entries[rotate_keep:]:
            try:
                gz_path.unlink()
                deleted += 1
            except OSError:
                logger.debug(
                    "log.rotate: delete failed for %s", gz_path, exc_info=True,
                )
                errors += 1

    return {"rotated": rotated, "deleted": deleted, "errors": errors}


def notification_staging_prune_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop flushed + silent notification_staging rows older than 30d.

    Pending digest rows are never pruned — they belong to a milestone
    that simply hasn't closed yet. Opens a short-lived work-service
    connection so the staging table is guaranteed to exist (the
    SQLiteWorkService init path runs the migration).
    """
    import sqlite3

    from pollypm.notification_staging import prune_old_staging

    with _load_config_and_store(payload) as (_config, store):
        retain_days = int(payload.get("retain_days") or 30)

        # The staging table lives in the shared state.db alongside work_*
        # tables; the work-service migration (v4) creates it. We open a
        # direct connection here because the prune is a pure DML op and
        # does not need the full service wrapper.
        db_path = getattr(store, "path", None) or _config.project.state_db
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            # Make sure the schema is present — safe no-op when the table
            # already exists (e.g. SQLiteWorkService ran migration v4).
            from pollypm.work.schema import create_work_tables
            create_work_tables(conn)
            summary = prune_old_staging(conn, retain_days=retain_days)
        finally:
            conn.close()

        # #349: audit lands on ``messages`` via the unified Store.
        msg_store = _open_msg_store(_config)
        try:
            if msg_store is not None:
                msg_store.append_event(
                    scope="system",
                    sender="system",
                    subject="notification_staging.prune",
                    payload={
                        "message": (
                            f"pruned {summary['flushed_pruned']} flushed + "
                            f"{summary['silent_pruned']} silent rows "
                            f"(>{retain_days}d)"
                        ),
                        **summary,
                    },
                )
        finally:
            _close_msg_store(msg_store)
        return summary


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def _register_handlers(api: JobHandlerAPI) -> None:
    api.register_handler(
        "session.health_sweep", session_health_sweep_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "capacity.probe", capacity_probe_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
    api.register_handler(
        "transcript.ingest", transcript_ingest_handler,
        max_attempts=2, timeout_seconds=30.0,
    )
    api.register_handler(
        "alerts.gc", alerts_gc_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "work.progress_sweep", work_progress_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    # #250 — semantic pane-text classifier. Detection + alerts only in
    # v1; send-keys interventions are deferred to a follow-up PR.
    api.register_handler(
        "pane.classify", pane_text_classify_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    # DB hygiene — incremental vacuum + memory TTL sweep. Both are cheap
    # daily sweeps that coordinate with the shared StateStore connection.
    api.register_handler(
        "db.vacuum", db_vacuum_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    api.register_handler(
        "memory.ttl_sweep", memory_ttl_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "events.retention_sweep", events_retention_sweep_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    api.register_handler(
        "notification_staging.prune", notification_staging_prune_handler,
        max_attempts=1, timeout_seconds=60.0,
    )
    # Harness agent-worktree hygiene — hourly prune of merged/stale
    # worktrees under ``<repo_root>/.claude/worktrees/agent-*``.
    api.register_handler(
        "agent_worktree.prune", agent_worktree_prune_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    # Log-file hygiene — hourly rotation + gzip of oversized logs.
    api.register_handler(
        "log.rotate", log_rotate_handler,
        max_attempts=1, timeout_seconds=120.0,
    )
    # Worker-worktree state audit — 10-minute classification of live
    # work_sessions worktrees (merge conflicts, lock files, orphans).
    # Issue #251.
    api.register_handler(
        "worktree.state_audit", worktree_state_audit_handler,
        max_attempts=1, timeout_seconds=120.0,
    )


def _register_roster(api: RosterAPI) -> None:
    # Cadences match the task spec for issue #164. ``inbox.sweep`` was
    # removed with the legacy inbox subsystem (see iv04).
    api.register_recurring("@every 10s", "session.health_sweep", {})
    api.register_recurring("@every 60s", "capacity.probe", {})
    api.register_recurring("@every 30s", "transcript.ingest", {})
    api.register_recurring("@every 5m", "alerts.gc", {})
    # #249 — work-service-aware stuck-task sweeper.
    api.register_recurring("@every 5m", "work.progress_sweep", {})
    # #250 — semantic pane-text classifier. 30s cadence is a balance
    # between operator latency (alerts within ~half a minute) and the
    # cost of capturing every active pane. The handler itself is cheap
    # (regex match per session) — the dominant cost is the tmux
    # capture-pane round trip.
    api.register_recurring("@every 30s", "pane.classify", {})
    # DB hygiene — daily around 4am local. Off-minute (``7``) avoids
    # fleet-wide sync if many cockpits run on the same host. Memory TTL
    # sweep runs a few minutes later so its writes don't collide with
    # the vacuum's page-reclaim pass.
    api.register_recurring("7 4 * * *", "db.vacuum", {}, dedupe_key="db.vacuum")
    api.register_recurring(
        "13 4 * * *", "memory.ttl_sweep", {}, dedupe_key="memory.ttl_sweep",
    )
    # Events-table retention — hourly at :37, off-pattern from the 4am
    # hygiene window and from the ``23``/``23 * * * *`` agent-worktree
    # prune. Tiered policy (audit 365d / operational 30d / high_volume
    # 7d / default 30d) lives in pollypm.storage.events_retention.
    api.register_recurring(
        "37 * * * *", "events.retention_sweep", {},
        dedupe_key="events.retention_sweep",
    )
    # Notification staging hygiene — flushed rollup rows and silent audit
    # rows older than 30 days are dropped. Pending digest rows are left
    # alone (they belong to a milestone that hasn't closed yet).
    api.register_recurring(
        "19 4 * * *", "notification_staging.prune", {},
        dedupe_key="notification_staging.prune",
    )
    # Harness agent-worktree hygiene — every hour at minute 23, off-pattern
    # from the 4am DB hygiene window. Only prunes merged branches; stale
    # unmerged trees are logged but left intact.
    api.register_recurring(
        "23 * * * *", "agent_worktree.prune", {},
        dedupe_key="agent_worktree.prune",
    )
    # Log-file hygiene — every hour at minute 31, off-pattern from the
    # agent-worktree prune (:23) and the 4am DB hygiene window. Rotates
    # any ``<logs_dir>/*.log`` over the configured threshold and keeps
    # only the most recent N gzipped rotations.
    api.register_recurring(
        "31 * * * *", "log.rotate", {},
        dedupe_key="log.rotate",
    )
    # Worker-worktree *state* audit — every 10 minutes. Complements the
    # hourly ``agent_worktree.prune`` (which GCs orphan worktrees) by
    # classifying live ``work_sessions`` worktrees into
    # clean/dirty/conflict/lock/detached/orphan and surfacing blockers
    # as alerts + inbox items. Issue #251.
    api.register_recurring("@every 10m", "worktree.state_audit", {})


plugin = PollyPMPlugin(
    name="core_recurring",
    version="0.1.0",
    description=(
        "Built-in recurring handlers — migrated from the old heartbeat loop. "
        "Registers inbox sweep, session health sweep, capacity probe, "
        "transcript ingest, alerts GC, and work-service progress sweep on "
        "the roster + job queue."
    ),
    capabilities=(
        Capability(kind="job_handler", name="inbox.sweep"),
        Capability(kind="job_handler", name="session.health_sweep"),
        Capability(kind="job_handler", name="capacity.probe"),
        Capability(kind="job_handler", name="transcript.ingest"),
        Capability(kind="job_handler", name="alerts.gc"),
        Capability(kind="job_handler", name="work.progress_sweep"),
        Capability(kind="job_handler", name="pane.classify"),
        Capability(kind="job_handler", name="db.vacuum"),
        Capability(kind="job_handler", name="memory.ttl_sweep"),
        Capability(kind="job_handler", name="events.retention_sweep"),
        Capability(kind="job_handler", name="notification_staging.prune"),
        Capability(kind="job_handler", name="agent_worktree.prune"),
        Capability(kind="job_handler", name="log.rotate"),
        Capability(kind="job_handler", name="worktree.state_audit"),
        Capability(kind="roster_entry", name="core_recurring"),
    ),
    register_handlers=_register_handlers,
    register_roster=_register_roster,
)
