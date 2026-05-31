"""Sweep-style recurring handlers for core_recurring."""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.inbox.kind import InboxItemKind

from .shared import (
    _close_msg_store,
    _load_config_and_store,
    _open_alert_exists,
    _open_msg_store,
)


logger = logging.getLogger(__name__)

_PANE_PATTERN_LIVE_TASK_STATUSES = (
    "draft",
    "queued",
    "in_progress",
    "rework",
    "blocked",
    "on_hold",
    "review",
)


def _resolve_sweep_target(
    *,
    task: Any,
    event: Any,
    work: Any,
    session_svc: Any,
    index: Any,
    counters: dict[str, int],
) -> str | None:
    """Resolve the target session for ``task`` or return None.

    Bumps ``counters[skipped_no_session]`` / ``counters[skipped_active_turn]``
    as appropriate. Returns the session ``target_name`` when the
    sweep should proceed, or None when it should skip this task.
    """
    handle = None
    if index is not None:
        try:
            handle = index.resolve(
                event.actor_type, event.actor_name, event.project,
            )
        except Exception:  # noqa: BLE001
            handle = None
    if handle is None:
        counters["skipped_no_session"] += 1
        return None
    target_name = getattr(handle, "name", "")
    if not target_name:
        counters["skipped_no_session"] += 1
        return None

    if session_svc is not None:
        checker = getattr(session_svc, "is_turn_active", None)
        if callable(checker):
            try:
                if bool(checker(target_name)):
                    counters["skipped_active_turn"] += 1
                    return None
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: is_turn_active probe failed for %s",
                    target_name, exc_info=True,
                )
    return target_name


def _record_state_drift(
    *,
    task: Any,
    target_name: str,
    drift: Any,
    msg_store: Any,
    state_store: Any,
    counters: dict[str, int],
) -> None:
    """Emit the drift audit event + upsert the drift alert."""
    audit_store = msg_store or state_store
    if audit_store is None:
        return
    current_node = getattr(task, "current_node_id", "") or ""
    message = (
        f"task {task.task_id}: observed "
        f"{drift.advance_to_node} deliverables, advancing "
        f"from {current_node} to {drift.advance_to_node} — "
        f"{drift.reason}"
    )
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
        logger.warning(
            "work.progress_sweep: state_drift audit emit failed for %s",
            task.task_id, exc_info=True,
        )
    alert_type = f"state_drift:{task.task_id}"
    try:
        is_new = not _open_alert_exists(
            msg_store=msg_store,
            state_store=state_store,
            session_name=target_name,
            alert_type=alert_type,
        )
        if msg_store is not None:
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
            counters["drift_alerted"] += 1
    except Exception:  # noqa: BLE001
        logger.warning(
            "work.progress_sweep: drift alert upsert failed for %s",
            task.task_id, exc_info=True,
        )


def _has_recent_session_event(
    *,
    target_name: str,
    msg_store: Any,
    state_store: Any,
    now: Any,
    stale_threshold_seconds: int,
) -> bool:
    """Return True when the target session has an event newer than the threshold."""
    from datetime import UTC, datetime, timedelta

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
            logger.debug(
                "work.progress_sweep: recent-event probe via msg_store failed for %s",
                target_name, exc_info=True,
            )
    if recent_ts is None and state_store is not None:
        recent_events = getattr(state_store, "recent_events", None)
        if callable(recent_events):
            try:
                for event_row in recent_events(limit=20):
                    if getattr(event_row, "session_name", None) != target_name:
                        continue
                    stamp = getattr(event_row, "created_at", None)
                    if stamp:
                        recent_ts = (
                            stamp.isoformat()
                            if hasattr(stamp, "isoformat")
                            else str(stamp)
                        )
                        break
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work.progress_sweep: recent-event probe via state_store failed for %s",
                    target_name, exc_info=True,
                )
    if recent_ts is None:
        return False
    try:
        last_ts = datetime.fromisoformat(recent_ts)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)
        return (now - last_ts) < timedelta(seconds=stale_threshold_seconds)
    except ValueError:
        return False


def _emit_resume_ping(
    *,
    event: Any,
    target_name: str,
    services: Any,
    work: Any,
    msg_store: Any,
    counters: dict[str, int],
) -> None:
    """Send the resume-ping notification and record bookkeeping."""
    from pollypm.task_assignment_notify import (
        DEDUPE_WINDOW_SECONDS,
        notify as _notify,
        record_sweeper_ping as _record_sweeper_ping,
    )

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
        return
    result = str(outcome.get("outcome", ""))
    _record_sweeper_ping(
        work,
        event.task_id,
        outcome=result,
        source="work.progress_sweep",
    )
    if result == "deduped":
        counters["deduped"] += 1
    elif result == "sent":
        counters["pinged"] += 1
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
                logger.warning(
                    "work.progress_sweep: stuck_on_task alert upsert "
                    "failed for %s/%s", target_name, event.task_id,
                    exc_info=True,
                )


def _work_progress_sweep_one(
    *,
    work: Any,
    services: Any,
    state_store: Any,
    msg_store: Any,
    session_svc: Any,
    sweep_config: Any,
    stale_threshold_seconds: int,
    counters: dict[str, int],
) -> bool:
    """Run one ``work.progress_sweep`` pass against a single work DB."""
    from datetime import UTC, datetime

    # #1365: recurring jobs use the shared task-assignment notification
    # surface, not the sibling task_assignment_notify plugin package.
    from pollypm.task_assignment_notify import (
        build_event_for_task as _build_event_for_task,
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

    try:
        tasks = work.list_tasks(work_status="in_progress")
    except Exception:  # noqa: BLE001
        logger.debug(
            "work.progress_sweep: list_tasks(in_progress) failed",
            exc_info=True,
        )
        return False

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
        if event.actor_type is ActorType.HUMAN:
            continue
        counters["considered"] += 1

        target_name = _resolve_sweep_target(
            task=task,
            event=event,
            work=work,
            session_svc=session_svc,
            index=index,
            counters=counters,
        )
        if target_name is None:
            continue

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
                state_store=msg_store or state_store,
                now=now,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "work.progress_sweep: drift reconcile failed for %s",
                task.task_id, exc_info=True,
            )
            drift = None
        if drift is not None:
            counters["drift_detected"] += 1
            _record_state_drift(
                task=task,
                target_name=target_name,
                drift=drift,
                msg_store=msg_store,
                state_store=state_store,
                counters=counters,
            )

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
                    counters["worker_blocking_questions"] += 1
                elif outcome == "reprompt":
                    counters["worker_reprompts"] += 1

        if _has_recent_session_event(
            target_name=target_name,
            msg_store=msg_store,
            state_store=state_store,
            now=now,
            stale_threshold_seconds=stale_threshold_seconds,
        ):
            counters["skipped_recent_event"] += 1
            continue

        _emit_resume_ping(
            event=event,
            target_name=target_name,
            services=services,
            work=work,
            msg_store=msg_store,
            counters=counters,
        )

    return True

def work_progress_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan in_progress tasks for staleness and emit resume pings (#249)."""
    # #1365: route through the shared helper module instead of a sibling
    # plugin API.
    from pollypm.task_assignment_notify import (
        auto_claim_enabled_for_project as _auto_claim_enabled_for_project,
        close_quietly as _close_quietly,
        load_runtime_services,
        open_project_work_service as _open_project_work_service,
        recover_dead_claims as _recover_dead_claims,
    )

    STALE_THRESHOLD_SECONDS = int(
        payload.get("stale_threshold_seconds") or 1800,
    )

    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    work = services.work_service
    state_store = services.state_store
    msg_store = services.msg_store
    session_svc = services.session_service

    if work is None:
        if not services.known_projects:
            return {"outcome": "skipped", "reason": "no_work_service"}

    counters = {
        "considered": 0,
        "pinged": 0,
        "skipped_active_turn": 0,
        "skipped_recent_event": 0,
        "skipped_no_session": 0,
        "deduped": 0,
        "drift_detected": 0,
        "drift_alerted": 0,
        "worker_blocking_questions": 0,
        "worker_reprompts": 0,
    }
    recovered_dead_claims = 0
    projects_scanned = 0
    projects_skipped = 0
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
        workspace_scanned = True
        if work is not None:
            workspace_scanned = _work_progress_sweep_one(
                work=work,
                services=services,
                state_store=state_store,
                msg_store=msg_store,
                session_svc=session_svc,
                sweep_config=sweep_config,
                stale_threshold_seconds=STALE_THRESHOLD_SECONDS,
                counters=counters,
            )
        elif not services.known_projects:
            return {"outcome": "skipped", "reason": "no_work_service"}

        for project in services.known_projects:
            project_work = _open_project_work_service(project, services)
            if project_work is None:
                projects_skipped += 1
                continue
            try:
                if _auto_claim_enabled_for_project(services, project):
                    recovery_totals = {"considered": 0, "by_outcome": {}}
                    _recover_dead_claims(
                        services, project_work, project, recovery_totals,
                    )
                    recovered_dead_claims += recovery_totals["by_outcome"].get(
                        "auto_claim_recovered", 0,
                    )
                project_ok = _work_progress_sweep_one(
                    work=project_work,
                    services=services,
                    state_store=state_store,
                    msg_store=msg_store,
                    session_svc=session_svc,
                    sweep_config=sweep_config,
                    stale_threshold_seconds=STALE_THRESHOLD_SECONDS,
                    counters=counters,
                )
                if project_ok:
                    projects_scanned += 1
                else:
                    projects_skipped += 1
            finally:
                _close_quietly(project_work)

        if not workspace_scanned and not services.known_projects:
            return {"outcome": "failed", "reason": "list_tasks_error"}
    finally:
        _close_quietly(work)

    return {
        "outcome": "swept",
        "considered": counters["considered"],
        "pinged": counters["pinged"],
        "deduped": counters["deduped"],
        "skipped_active_turn": counters["skipped_active_turn"],
        "skipped_recent_event": counters["skipped_recent_event"],
        "skipped_no_session": counters["skipped_no_session"],
        "drift_detected": counters["drift_detected"],
        "drift_alerted": counters["drift_alerted"],
        "worker_blocking_questions": counters["worker_blocking_questions"],
        "worker_reprompts": counters["worker_reprompts"],
        "recovered_dead_claims": recovered_dead_claims,
        "projects_scanned": projects_scanned,
        "projects_skipped": projects_skipped,
    }


def pane_text_classify_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Semantic pane-text classifier sweep — issue #250."""
    # ``load_runtime_services`` is shared runtime plumbing; avoid coupling
    # this recurring plugin to task_assignment_notify's plugin package.
    from pollypm.task_assignment_notify import (
        load_runtime_services,
    )

    config_path_hint = payload.get("config_path")
    config_path = Path(config_path_hint) if config_path_hint else None
    services = load_runtime_services(config_path=config_path)
    # #1069 — release the StateStore + work-service connections that
    # ``load_runtime_services`` opens on every call. Without this
    # finally the @every 30s ``pane.classify`` cadence leaked sqlite
    # connections at the same rate as ``task_assignment.sweep``.
    try:
        return _pane_text_classify_body(
            services=services,
            payload=payload,
        )
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "pane_text_classify: services.close raised", exc_info=True,
            )


def _pane_text_classify_body(
    *,
    services: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Inner body of :func:`pane_text_classify_handler`.

    Split out so the outer handler can guarantee
    :meth:`_RuntimeServices.close` runs on every return path (#1069).
    """
    from pollypm.recovery.pane_patterns import (
        RULES,
        USER_VISIBLE_RULES,
        classify_pane,
        context_truncation_trigger,
        rule_by_name,
    )

    session_svc = services.session_service
    state_store = services.state_store
    msg_store = services.msg_store
    work_service = services.work_service

    if session_svc is None or state_store is None:
        return {"outcome": "skipped", "reason": "services_unavailable"}

    capture_lines = int(payload.get("capture_lines", 200) or 200)
    all_rule_names = [rule.name for rule in RULES]

    sessions_scanned = 0
    alerts_raised = 0
    alerts_cleared = 0
    inbox_items_emitted = 0
    inbox_items_resolved = 0
    context_audit_events_emitted = 0
    capture_failures = 0
    pm_turn_transitions = 0  # #1633 — active → ended PM turn flips this tick.
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

        # #1633 — detect PM-persona turn-ended transitions inline so we
        # piggyback on the existing pane capture rather than spinning a
        # second sweep. ``record_turn_state`` emits the ``pm.turn_ended``
        # audit event exactly once per active → ended transition and
        # persists the per-session state for the rail glyph override.
        try:
            from pollypm.pm_turn_state import (
                detect_pm_turn_ended,
                is_pm_session,
                record_turn_state,
            )

            if is_pm_session(session_name):
                turn_ended = detect_pm_turn_ended(pane_text)
                if record_turn_state(session_name, turn_ended=turn_ended):
                    pm_turn_transitions += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "pane_text_classify: pm_turn_state record failed for %s",
                session_name, exc_info=True,
            )

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
                    is_new = not _open_alert_exists(
                        msg_store=msg_store,
                        state_store=state_store,
                        session_name=session_name,
                        alert_type=alert_type,
                    )
                    if msg_store is not None:
                        msg_store.upsert_alert(
                            session_name, alert_type, severity, message,
                        )
                    else:
                        state_store.upsert_alert(
                            session_name, alert_type, severity, message,
                        )
                    if is_new:
                        alerts_raised += 1
                        match_counts[rule_name] += 1
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
                            logger.debug(
                                "pane_text_classify: pane.classify.match audit emit "
                                "failed for %s/%s", session_name, rule_name,
                                exc_info=True,
                            )
                        if rule_name == "context_full":
                            if _emit_context_truncated_audit_event(
                                session_name=session_name,
                                trigger=(
                                    context_truncation_trigger(pane_text)
                                    or "context_full"
                                ),
                                pane_text=pane_text,
                            ):
                                context_audit_events_emitted += 1
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "pane_text_classify: upsert_alert failed "
                        "for %s/%s", session_name, rule_name,
                        exc_info=True,
                    )

                if rule_name in USER_VISIBLE_RULES and work_service is not None:
                    emitted = _emit_pane_pattern_inbox_item(
                        work_service=work_service,
                        session_name=session_name,
                        rule_name=rule_name,
                        pane_text=pane_text,
                        project_key=_pane_pattern_project_key(
                            services=services,
                            handle=handle,
                            session_name=session_name,
                        ),
                        state_store=state_store,
                        msg_store=msg_store,
                    )
                    if emitted:
                        inbox_items_emitted += 1
            else:
                try:
                    if _open_alert_exists(
                        msg_store=msg_store,
                        state_store=state_store,
                        session_name=session_name,
                        alert_type=alert_type,
                    ):
                        if msg_store is not None:
                            msg_store.clear_alert(session_name, alert_type)
                        else:
                            state_store.clear_alert(session_name, alert_type)
                        alerts_cleared += 1
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "pane_text_classify: clear_alert failed for %s/%s",
                        session_name, alert_type, exc_info=True,
                    )
                if rule_name == "ask_user_decision" and work_service is not None:
                    inbox_items_resolved += _resolve_pane_pattern_inbox_items(
                        work_service=work_service,
                        session_name=session_name,
                        rule_name=rule_name,
                        project_key=_pane_pattern_project_key(
                            services=services,
                            handle=handle,
                            session_name=session_name,
                        ),
                        msg_store=msg_store,
                    )

    return {
        "outcome": "swept",
        "sessions_scanned": sessions_scanned,
        "alerts_raised": alerts_raised,
        "alerts_cleared": alerts_cleared,
        "inbox_items_emitted": inbox_items_emitted,
        "inbox_items_resolved": inbox_items_resolved,
        "context_audit_events_emitted": context_audit_events_emitted,
        "capture_failures": capture_failures,
        "match_counts": match_counts,
        # #1633 — count of PM personas that just transitioned to
        # awaiting-user this tick. Each non-zero value corresponds to
        # one ``pm.turn_ended`` audit event.
        "pm_turn_transitions": pm_turn_transitions,
    }


def _emit_context_truncated_audit_event(
    *,
    session_name: str,
    trigger: str,
    pane_text: str,
) -> bool:
    """Emit the canonical audit row for context-limit pane detections."""
    try:
        from pollypm.audit.log import (
            EVENT_AGENT_CONTEXT_TRUNCATED,
            emit as audit_emit,
        )

        audit_emit(
            event=EVENT_AGENT_CONTEXT_TRUNCATED,
            project="_workspace",
            subject=session_name,
            actor="audit_watchdog",
            status="warn",
            metadata={
                "trigger": trigger,
                "pane_excerpt": (pane_text or "")[-200:],
            },
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "pane_text_classify: agent.context.truncated audit emit failed "
            "for %s", session_name,
            exc_info=True,
        )
        return False


def _pane_pattern_project_key(
    *,
    services: Any,
    handle: Any,
    session_name: str,
) -> str:
    """Best-effort project routing for pane-pattern inbox rows."""
    try:
        from pollypm.work.task_state import parse_task_window_name

        parsed = parse_task_window_name(session_name)
    except Exception:  # noqa: BLE001
        parsed = None
    if parsed is not None:
        project_key, _task_number = parsed
        if project_key:
            return str(project_key)

    config = getattr(services, "config", None)
    sessions = getattr(config, "sessions", None) or {}
    session_cfg = sessions.get(session_name)
    if session_cfg is None:
        window_name = getattr(handle, "window_name", "") or ""
        session_cfg = next(
            (
                candidate for candidate in sessions.values()
                if getattr(candidate, "window_name", None) == window_name
                or getattr(candidate, "name", None) == session_name
            ),
            None,
        )
    if session_cfg is not None:
        project_key = str(getattr(session_cfg, "project", "") or "")
        if project_key:
            return project_key

    projects = getattr(config, "projects", None) or {}
    cwd = getattr(handle, "cwd", "") or ""
    if cwd:
        try:
            cwd_path = Path(cwd).resolve()
        except (OSError, RuntimeError):
            cwd_path = Path(cwd)
        for key, project in projects.items():
            project_path_raw = getattr(project, "path", None)
            if project_path_raw is None:
                continue
            try:
                project_path = Path(project_path_raw).resolve()
            except (OSError, RuntimeError):
                project_path = Path(project_path_raw)
            try:
                cwd_path.relative_to(project_path)
            except ValueError:
                continue
            return str(key)

    return "inbox"


def _open_pane_pattern_inbox_tasks(
    *,
    work_service: Any,
    dedupe_label: str,
    target_project: str,
) -> list[Any]:
    list_fn = getattr(work_service, "list_tasks", None)
    if not callable(list_fn):
        return []

    matches: list[Any] = []
    seen_task_ids: set[str] = set()
    for status in _PANE_PATTERN_LIVE_TASK_STATUSES:
        try:
            tasks = list_fn(work_status=status, project=target_project)
        except TypeError:
            tasks = list_fn(work_status=status)
        for task in tasks or []:
            labels = {str(label) for label in (getattr(task, "labels", None) or [])}
            if dedupe_label not in labels:
                continue
            task_project = str(getattr(task, "project", "") or "")
            if task_project and task_project != target_project:
                continue
            task_id = str(getattr(task, "task_id", "") or "")
            if task_id and task_id in seen_task_ids:
                continue
            if task_id:
                seen_task_ids.add(task_id)
            matches.append(task)
    return matches


def _resolve_pane_pattern_inbox_items(
    *,
    work_service: Any,
    session_name: str,
    rule_name: str,
    project_key: str,
    msg_store: Any = None,
) -> int:
    if rule_name != "ask_user_decision":
        return 0

    archive_fn = getattr(work_service, "archive_task", None)
    if not callable(archive_fn):
        logger.debug(
            "pane_text_classify: archive_task unavailable for resolved %s/%s",
            session_name, rule_name,
        )
        return 0

    dedupe_label = f"pane_pattern:{rule_name}:{session_name}"
    target_project = project_key
    try:
        candidates = _open_pane_pattern_inbox_tasks(
            work_service=work_service,
            dedupe_label=dedupe_label,
            target_project=target_project,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "pane_text_classify: resolved inbox scan failed for %s",
            dedupe_label, exc_info=True,
        )
        return 0

    resolved = 0
    for task in candidates:
        task_id = str(getattr(task, "task_id", "") or "")
        if not task_id:
            continue
        try:
            archive_fn(task_id, actor="pane_text_classify", strict=False)
        except Exception:  # noqa: BLE001
            logger.debug(
                "pane_text_classify: resolved inbox archive failed for %s",
                task_id, exc_info=True,
            )
            continue
        resolved += 1
        if msg_store is not None:
            try:
                msg_store.append_event(
                    scope=session_name,
                    sender=session_name,
                    subject="pane.classify.inbox_resolved",
                    payload={
                        "message": (
                            f"resolved inbox task {task_id} after "
                            f"rule '{rule_name}' cleared"
                        ),
                        "task_id": task_id,
                        "rule": rule_name,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "pane_text_classify: inbox_resolved audit append failed "
                    "for %s/%s",
                    session_name,
                    rule_name,
                    exc_info=True,
                )
    return resolved


def _emit_pane_pattern_inbox_item(
    *,
    work_service: Any,
    session_name: str,
    rule_name: str,
    pane_text: str,
    project_key: str = "inbox",
    state_store: Any = None,
    msg_store: Any = None,
) -> bool:
    """Create a user-visible inbox task for a matched pane pattern."""
    dedupe_label = f"pane_pattern:{rule_name}:{session_name}"
    target_project = project_key if rule_name == "ask_user_decision" else "inbox"

    try:
        if _open_pane_pattern_inbox_tasks(
            work_service=work_service,
            dedupe_label=dedupe_label,
            target_project=target_project,
        ):
            return False
    except Exception:  # noqa: BLE001
        logger.debug(
            "pane_text_classify: dedupe scan failed for %s",
            dedupe_label, exc_info=True,
        )

    title_map = {
        "context_full": (
            f"Session '{session_name}' approaching context limit — "
            f"consider /compact"
        ),
        "permission_prompt": (
            f"Session '{session_name}' is waiting on a permission "
            f"prompt — approval needed"
        ),
        "ask_user_decision": (
            f"{target_project} PM is waiting on a decision from you"
            if target_project != "inbox"
            else f"Session '{session_name}' is waiting on a decision from you"
        ),
    }
    title = title_map.get(
        rule_name,
        f"Session '{session_name}' matched pane pattern '{rule_name}'",
    )

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
    elif rule_name == "ask_user_decision":
        body_parts.extend([
            "- Open the agent session from the cockpit or chat surface "
            "and answer the interactive decision prompt.",
            "- This is a visibility backstop: PollyPM detected the "
            "AskUserQuestion chrome, but did not parse the options for "
            "in-UI answering.",
        ])
    body_parts.extend([
        "",
        f"Alert type: `pane:{rule_name}`. This inbox item was emitted "
        "by the pane-text classifier (issues #250/#2493).",
    ])
    body = "\n".join(body_parts)

    labels = [
        "pane_pattern",
        f"rule:{rule_name}",
        f"session:{session_name}",
        dedupe_label,
    ]
    if rule_name == "ask_user_decision":
        labels.append("ask_user_decision")

    is_ask_user_decision = rule_name == "ask_user_decision"

    try:
        # Most pane-pattern findings go to Polly to recover. The
        # AskUserQuestion backstop is different: it is explicitly a
        # human decision visibility row when structured option capture
        # failed, so it is routed to the user inbox.
        inbox_task = work_service.create(
            title=title,
            description=body,
            type="task",
            project=target_project,
            flow_template="chat",
            roles=(
                {"requester": "user", "actor": session_name}
                if is_ask_user_decision
                else {"requester": session_name, "operator": "polly"}
            ),
            priority="normal",
            created_by=session_name,
            labels=labels,
            kind=(
                InboxItemKind.PM_QUESTION_UNANSWERED.value
                if is_ask_user_decision
                else InboxItemKind.ACTIVITY_EVENT.value
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "pane_text_classify: inbox create failed for %s/%s",
            session_name, rule_name, exc_info=True,
        )
        return False

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
            logger.debug(
                "pane_text_classify: inbox_emitted audit append failed for %s/%s",
                session_name, rule_name, exc_info=True,
            )
    return True


def _worktree_audit_clear_clean(
    *,
    session_key: str,
    task_id: str,
    msg_store: Any,
    store: Any,
    state_alert_types: tuple[str, ...],
    alert_exists_fn,
) -> int:
    """Clear every stale ``worktree_state:*`` alert for a now-clean worktree."""
    cleared = 0
    for kind in state_alert_types:
        alert_type = f"worktree_state:{task_id}:{kind}"
        if not alert_exists_fn(session_key, alert_type):
            continue
        try:
            (msg_store or store).clear_alert(session_key, alert_type)
            cleared += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "worktree.state_audit: clear_alert failed for %s/%s",
                session_key, alert_type, exc_info=True,
            )
    return cleared


def _worktree_audit_handle_merge_conflict(
    *,
    classification,
    wt_path: Path,
    sess,
    agent: str,
    session_key: str,
    task_id: str,
    msg_store: Any,
    store: Any,
    work: Any,
) -> tuple[int, int]:
    """Raise the merge-conflict alert and emit the inbox task."""
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
    _raise_alert(msg_store or store, session_key, alert_type, "error", message)
    fix_hint = (
        f"Run `git -C {wt_path} status` to inspect the conflict, "
        f"then resolve and `git commit` or reassign the task."
    )
    file_word = "file" if len(files) == 1 else "files"
    body = (
        f"Worker {agent} hit a merge conflict in {wt_path} while "
        f"working on task {task_id}.\n\n"
        f"{len(files)} conflicted {file_word} detected.\n\n"
        f"Fix: {fix_hint}"
    )
    emitted = 1 if _emit_inbox_task(
        work,
        subject=f"Merge conflict: {task_id}",
        body=body,
        actor=agent,
        dedupe_label=f"worktree_audit:{task_id}:merge_conflict",
        project=sess.task_project,
    ) else 0
    return 1, emitted


def _worktree_audit_handle_dirty_stale(
    *,
    classification,
    wt_path: Path,
    agent: str,
    session_key: str,
    task_id: str,
    msg_store: Any,
    store: Any,
    now_epoch: float,
    dirty_stale_seconds: int,
    alert_exists_fn,
) -> tuple[int, int]:
    """Either raise or clear the dirty-stale alert based on mtime age."""
    try:
        mtime = wt_path.stat().st_mtime
    except OSError:
        mtime = now_epoch
    age_s = now_epoch - mtime
    alert_type = f"worktree_state:{task_id}:dirty_stale"
    if age_s >= dirty_stale_seconds:
        message = (
            f"{agent}: {wt_path} has uncommitted changes and "
            f"hasn't been touched in ~{int(age_s // 60)}min "
            f"(task {task_id}). Fix: check in on the worker \u2014 "
            f"likely stuck or idle."
        )
        _raise_alert(msg_store or store, session_key, alert_type, "warn", message)
        return 1, 0
    if alert_exists_fn(session_key, alert_type):
        try:
            (msg_store or store).clear_alert(session_key, alert_type)
            return 0, 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "worktree.state_audit: clear_alert failed for %s/%s",
                session_key, alert_type, exc_info=True,
            )
    return 0, 0


_TERMINAL_TASK_STATUS_VALUES = frozenset({"done", "cancelled"})
_TASK_WORKTREE_BRANCH_PREFIXES = ("task/",)


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _worktree_audit_task_is_terminal(work: Any, task_id: str) -> bool:
    try:
        task = work.get(task_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "worktree.state_audit: could not read task %s for orphan cleanup",
            task_id,
            exc_info=True,
        )
        return False
    return _status_value(getattr(task, "work_status", None)) in _TERMINAL_TASK_STATUS_VALUES


def _git_for_worktree_cleanup(
    repo_root: Path,
    *args: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _remove_orphan_task_worktree(
    *,
    repo_root: Path,
    wt_path: Path,
    branch: str | None,
) -> bool:
    """Remove a terminal task's leftover git worktree and branch."""
    if not wt_path.exists():
        return True
    result = _git_for_worktree_cleanup(
        repo_root, "worktree", "remove", "--force", str(wt_path),
    )
    if result.returncode != 0 and "locked" in (result.stderr or "").lower():
        _git_for_worktree_cleanup(repo_root, "worktree", "unlock", str(wt_path))
        result = _git_for_worktree_cleanup(
            repo_root, "worktree", "remove", "--force", str(wt_path),
        )
    if result.returncode != 0:
        logger.warning(
            "worktree.state_audit: failed to remove orphan task worktree %s: %s",
            wt_path,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    _git_for_worktree_cleanup(repo_root, "worktree", "prune", timeout=60)
    if branch and branch.startswith(_TASK_WORKTREE_BRANCH_PREFIXES):
        _git_for_worktree_cleanup(repo_root, "branch", "-D", branch, timeout=60)
    return True


def _mark_orphan_worker_session_ended(work: Any, sess: Any) -> None:
    marker = getattr(work, "mark_worker_session_ended", None)
    if not callable(marker):
        return
    try:
        marker(
            task_project=sess.task_project,
            task_number=int(sess.task_number),
            ended_at=datetime.now(UTC).isoformat(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "worktree.state_audit: mark_worker_session_ended failed for %s/%s",
            getattr(sess, "task_project", ""),
            getattr(sess, "task_number", ""),
            exc_info=True,
        )


def _close_orphan_branch_inbox_tasks(
    work: Any,
    *,
    project: str,
    dedupe_label: str,
) -> int:
    closed = 0
    cancel = getattr(work, "cancel", None)
    if not callable(cancel):
        return closed
    try:
        candidates = []
        for status in ("draft", "queued", "in_progress"):
            candidates.extend(work.list_tasks(project=project, work_status=status))
    except Exception:  # noqa: BLE001
        logger.debug(
            "worktree.state_audit: orphan inbox cleanup scan failed for %s",
            dedupe_label,
            exc_info=True,
        )
        return closed
    for task in candidates:
        labels = getattr(task, "labels", None) or ()
        if dedupe_label not in labels:
            continue
        try:
            cancel(
                getattr(task, "task_id"),
                "worktree.state_audit",
                "routine orphan worktree cleanup auto-closed",
            )
            closed += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "worktree.state_audit: orphan inbox cleanup cancel failed for %s",
                getattr(task, "task_id", dedupe_label),
                exc_info=True,
            )
    return closed


def _worktree_audit_handle_orphan_branch(
    *,
    classification,
    wt_path: Path,
    sess,
    agent: str,
    session_key: str,
    task_id: str,
    repo_root: Path,
    msg_store: Any,
    store: Any,
    work: Any,
) -> tuple[int, int, int, int, int]:
    """Self-heal terminal orphan worktrees; keep any remainder non-inbox."""
    age_days = float(classification.metadata.get("age_days", 0.0))
    alert_type = f"worktree_state:{task_id}:orphan_branch"
    dedupe_label = f"worktree_audit:{task_id}:orphan_branch"
    if _worktree_audit_task_is_terminal(work, task_id):
        removed = _remove_orphan_task_worktree(
            repo_root=repo_root,
            wt_path=wt_path,
            branch=classification.branch,
        )
        closed = _close_orphan_branch_inbox_tasks(
            work,
            project=sess.task_project,
            dedupe_label=dedupe_label,
        )
        if removed:
            _mark_orphan_worker_session_ended(work, sess)
            try:
                (msg_store or store).clear_alert(session_key, alert_type)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "worktree.state_audit: clear orphan alert failed for %s/%s",
                    session_key,
                    alert_type,
                    exc_info=True,
                )
            return 0, 0, 1, 1, closed

    message = (
        f"{agent}: {wt_path} on local-only branch "
        f"{classification.branch or '(unknown)'} with no upstream "
        f"and no commit in ~{age_days:.1f}d (task {task_id}). "
        f"Maintenance: the prune handler will reap it once the task is terminal."
    )
    _raise_alert(msg_store or store, session_key, alert_type, "info", message)
    closed = _close_orphan_branch_inbox_tasks(
        work,
        project=sess.task_project,
        dedupe_label=dedupe_label,
    )
    return 1, 0, 0, 0, closed


def worktree_state_audit_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify every active worker-session worktree + surface blockers (#251)."""
    import time as _time

    from pollypm.worktree_audit import (
        WorktreeState,
        classify_worktree_state,
    )

    with _load_config_and_store(payload) as (config, store):
        msg_store = _open_msg_store(config)

        def _alert_exists(session_name: str, alert_type: str) -> bool:
            if msg_store is None:
                return False
            try:
                rows = msg_store.query_messages(
                    type="alert",
                    state="open",
                    scope=session_name,
                    sender=alert_type,
                    limit=1,
                )
            except Exception:  # noqa: BLE001
                return False
            return bool(rows)

        try:
            from pollypm.projects import project_state_db_path
            from pollypm.work import create_work_service

            project_root = config.project.root_dir
            db_path = project_state_db_path(project_root)
            work = create_work_service(db_path=db_path, project_path=project_root)
        except Exception:  # noqa: BLE001
            logger.debug(
                "worktree.state_audit: work service unavailable", exc_info=True,
            )
            _close_msg_store(msg_store)
            return {"outcome": "skipped", "reason": "no_work_service"}

        STATE_ALERT_TYPES: tuple[str, ...] = (
            "merge_conflict", "lock_file", "detached_head",
            "dirty_stale", "orphan_branch",
        )

        considered = 0
        classified: dict[str, int] = {}
        alerts_raised = 0
        alerts_cleared = 0
        inbox_emitted = 0
        worktrees_pruned = 0
        inbox_closed = 0
        LOCK_ESCALATE_SECONDS = 5 * 60
        DIRTY_STALE_SECONDS = 60 * 60
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

                if state in (WorktreeState.CLEAN, WorktreeState.MISSING):
                    alerts_cleared += _worktree_audit_clear_clean(
                        session_key=session_key,
                        task_id=task_id,
                        msg_store=msg_store,
                        store=store,
                        state_alert_types=STATE_ALERT_TYPES,
                        alert_exists_fn=_alert_exists,
                    )
                    continue

                if state is WorktreeState.MERGE_CONFLICT:
                    raised, emitted = _worktree_audit_handle_merge_conflict(
                        classification=classification,
                        wt_path=wt_path,
                        sess=sess,
                        agent=agent,
                        session_key=session_key,
                        task_id=task_id,
                        msg_store=msg_store,
                        store=store,
                        work=work,
                    )
                    alerts_raised += raised
                    inbox_emitted += emitted

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
                    _raise_alert(msg_store or store, session_key, alert_type, severity, message)
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
                    _raise_alert(msg_store or store, session_key, alert_type, "warn", message)
                    alerts_raised += 1

                elif state is WorktreeState.DIRTY_EXPECTED:
                    raised, cleared = _worktree_audit_handle_dirty_stale(
                        classification=classification,
                        wt_path=wt_path,
                        agent=agent,
                        session_key=session_key,
                        task_id=task_id,
                        msg_store=msg_store,
                        store=store,
                        now_epoch=now_epoch,
                        dirty_stale_seconds=DIRTY_STALE_SECONDS,
                        alert_exists_fn=_alert_exists,
                    )
                    alerts_raised += raised
                    alerts_cleared += cleared

                elif state is WorktreeState.ORPHAN_BRANCH:
                    raised, emitted, cleared, pruned, closed = (
                        _worktree_audit_handle_orphan_branch(
                            classification=classification,
                            wt_path=wt_path,
                            sess=sess,
                            agent=agent,
                            session_key=session_key,
                            task_id=task_id,
                            repo_root=project_root,
                            msg_store=msg_store,
                            store=store,
                            work=work,
                        )
                    )
                    alerts_raised += raised
                    inbox_emitted += emitted
                    alerts_cleared += cleared
                    worktrees_pruned += pruned
                    inbox_closed += closed
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
            "worktrees_pruned": worktrees_pruned,
            "inbox_closed": inbox_closed,
        }

def _raise_alert(
    store: Any, session_name: str, alert_type: str, severity: str, message: str,
) -> None:
    """Thin wrapper that swallows a failing alert write."""
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
    """Create a user-routed inbox task on the chat flow."""
    try:
        # Inbox tasks land on the ``chat`` flow which starts in
        # ``draft`` status pending user promotion — so the dedupe scan
        # MUST include ``draft`` alongside ``queued`` and ``in_progress``,
        # otherwise every recurring sweep re-creates the same task and
        # the project accumulates stuck_draft churn (one orphan branch
        # observed producing 30 dupes in 10h, all draft). #2021
        existing = work.list_tasks(project=project, work_status="draft")
        existing += work.list_tasks(project=project, work_status="queued")
        existing += work.list_tasks(project=project, work_status="in_progress")
        for task in existing:
            labels = getattr(task, "labels", None) or ()
            if dedupe_label in labels:
                return False
    except Exception:  # noqa: BLE001
        logger.debug(
            "worktree.state_audit: inbox dedupe scan failed for %s",
            dedupe_label, exc_info=True,
        )

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
