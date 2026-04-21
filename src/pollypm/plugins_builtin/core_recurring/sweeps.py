"""Sweep-style recurring handlers for core_recurring."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .shared import (
    _close_msg_store,
    _load_config,
    _load_config_and_store,
    _open_alert_exists,
    _open_msg_store,
)


logger = logging.getLogger(__name__)


def work_progress_sweep_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan in_progress tasks for staleness and emit resume pings (#249)."""
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
        return {"outcome": "skipped", "reason": "no_work_service"}

    considered = 0
    pinged = 0
    skipped_active_turn = 0
    skipped_recent_event = 0
    skipped_no_session = 0
    deduped = 0
    drift_detected = 0
    drift_alerted = 0
    worker_blocking_questions = 0
    worker_reprompts = 0
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
            considered += 1

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

            if session_svc is not None:
                checker = getattr(session_svc, "is_turn_active", None)
                if callable(checker):
                    try:
                        if bool(checker(target_name)):
                            skipped_active_turn += 1
                            continue
                    except Exception:  # noqa: BLE001
                        pass

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
                drift_detected += 1
                current_node = getattr(task, "current_node_id", "") or ""
                message = (
                    f"task {task.task_id}: observed "
                    f"{drift.advance_to_node} deliverables, advancing "
                    f"from {current_node} to {drift.advance_to_node} — "
                    f"{drift.reason}"
                )
                audit_store = msg_store or state_store
                if audit_store is not None:
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
                            drift_alerted += 1
                    except Exception:  # noqa: BLE001
                        pass

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
    """Semantic pane-text classifier sweep — issue #250."""
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
                            pass
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
    """Create a user-visible inbox task for a matched pane pattern."""
    dedupe_label = f"pane_pattern:{rule_name}:{session_name}"

    try:
        list_fn = getattr(work_service, "list_tasks", None)
        if callable(list_fn):
            for status in ("queued", "in_progress", "draft", "review"):
                try:
                    tasks = list_fn(work_status=status, project="inbox")
                except TypeError:
                    tasks = list_fn(work_status=status)
                for task in tasks or []:
                    labels = getattr(task, "labels", None) or []
                    if dedupe_label in labels:
                        return False
    except Exception:  # noqa: BLE001
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

        STATE_ALERT_TYPES: tuple[str, ...] = (
            "merge_conflict", "lock_file", "detached_head",
            "dirty_stale", "orphan_branch",
        )

        considered = 0
        classified: dict[str, int] = {}
        alerts_raised = 0
        alerts_cleared = 0
        inbox_emitted = 0
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
                    for kind in STATE_ALERT_TYPES:
                        alert_type = f"worktree_state:{task_id}:{kind}"
                        if not _alert_exists(session_key, alert_type):
                            continue
                        try:
                            (msg_store or store).clear_alert(session_key, alert_type)
                            alerts_cleared += 1
                        except Exception:  # noqa: BLE001
                            pass
                    continue

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
                    _raise_alert(msg_store or store, session_key, alert_type, "error", message)
                    alerts_raised += 1
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
                        _raise_alert(msg_store or store, session_key, alert_type, "warn", message)
                        alerts_raised += 1
                    else:
                        if _alert_exists(session_key, alert_type):
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
                    _raise_alert(msg_store or store, session_key, alert_type, "info", message)
                    alerts_raised += 1
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
        existing = work.list_tasks(project=project, work_status="queued")
        existing += work.list_tasks(project=project, work_status="in_progress")
        for task in existing:
            labels = getattr(task, "labels", None) or ()
            if dedupe_label in labels:
                return False
    except Exception:  # noqa: BLE001
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
