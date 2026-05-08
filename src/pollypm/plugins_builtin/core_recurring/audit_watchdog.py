"""``audit.watchdog`` cadence handler — surface forensic findings.

Born from the savethenovel post-mortem (2026-05-06): the audit log
(#1342) records every task / marker mutation; this handler reads
those events on a fixed cadence and surfaces "broken state" findings
via the existing ``upsert_alert`` channel — the same surface used by
``blocked_chain.sweep`` (#1073) and the supervisor's session-health
sweep. No new alert channel is introduced.

The handler is a thin wrapper around :mod:`pollypm.audit.watchdog`,
which holds the pure detectors. The heartbeat scheduler invokes
``audit_watchdog_handler`` every 5 minutes; per-tick behaviour:

1. Emit a ``heartbeat.tick`` audit event so liveness is observable.
2. For each known project, read the last ``window_seconds * 2`` of
   audit events and run the four detectors.
3. For every finding, emit an ``audit.finding`` audit event AND
   upsert an alert keyed by ``(rule, project, subject)``.

Idempotent across ticks because :meth:`upsert_alert` collapses
repeats. The emitted ``audit.finding`` events are deliberately
duplicated each tick — the audit log is append-only and operators
want to see "this fired N ticks in a row" as a signal of severity.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pollypm.audit.watchdog import (
    ESCALATION_THROTTLE_SECONDS,
    Finding,
    RULE_ROLE_SESSION_MISSING,
    RULE_STUCK_DRAFT,
    RULE_TASK_ON_HOLD_STALE,
    RULE_TASK_REVIEW_STALE,
    RULE_WORKER_SESSION_DEAD_LOOP,
    WATCHDOG_ALERT_TYPE,
    WatchdogConfig,
    emit_escalation_dispatched,
    emit_finding,
    emit_heartbeat_tick,
    format_finding_message,
    format_unstick_brief,
    scan_project,
    was_recently_dispatched,
    watchdog_alert_session_name,
)


# #1414 — only the auto-unstick rules are eligible for architect dispatch.
# Legacy forensic rules (orphan_marker, marker_leaked, etc.) already have
# operator-actionable alerts and predate this dispatch path; we leave
# them additive-only to keep the blast radius small.
_DISPATCHABLE_RULES: frozenset[str] = frozenset({
    # #1440 — stale drafts are architect-originated planning stalls. Alerting
    # alone leaves them parked forever; dispatch the architect to queue,
    # cancel, or rewrite them.
    RULE_STUCK_DRAFT,
    RULE_TASK_REVIEW_STALE,
    RULE_ROLE_SESSION_MISSING,
    RULE_WORKER_SESSION_DEAD_LOOP,
    # #1424 — on_hold escalation lands in the architect's pane with the
    # reviewer's rationale folded into the brief. Default first responder
    # is the architect; only the architect calls ``pm notify`` if the
    # issue genuinely needs human judgement.
    RULE_TASK_ON_HOLD_STALE,
})


logger = logging.getLogger(__name__)


__all__ = [
    "audit_watchdog_handler",
    "AUDIT_WATCHDOG_HANDLER_NAME",
    "AUDIT_WATCHDOG_SCHEDULE",
]


AUDIT_WATCHDOG_HANDLER_NAME = "audit.watchdog"
# Every 5 minutes is the same cadence as ``stuck_claims.sweep`` /
# ``alerts.gc`` — enough to catch the savethenovel-class symptom
# within ~5 min of occurrence without spamming the queue.
AUDIT_WATCHDOG_SCHEDULE = "@every 5m"


def _route_to_alert_sink(
    finding: Finding,
    *,
    msg_store: Any,
    state_store: Any,
) -> bool:
    """Upsert a finding via the unified Store, falling back to StateStore.

    Mirrors ``blocked_chain._emit_alert``: prefer the unified
    ``msg_store`` (post-#349 path) and fall back to the legacy
    ``StateStore`` so the cadence still works on installs that
    haven't migrated yet.
    """
    target = msg_store or state_store
    if target is None:
        return False
    upsert = getattr(target, "upsert_alert", None)
    if not callable(upsert):
        return False
    session_name = watchdog_alert_session_name(
        finding.rule, finding.project, finding.subject,
    )
    try:
        upsert(
            session_name,
            WATCHDOG_ALERT_TYPE,
            finding.severity,
            format_finding_message(finding),
        )
        return True
    except Exception:  # noqa: BLE001 — alert path failures must not crash the sweep
        logger.debug(
            "audit.watchdog: upsert_alert failed for %s/%s",
            finding.rule, finding.project, exc_info=True,
        )
        return False


def _config_from_payload(payload: dict[str, Any]) -> WatchdogConfig:
    """Build a :class:`WatchdogConfig` from the handler payload."""
    if not isinstance(payload, dict):
        return WatchdogConfig()
    kwargs: dict[str, int] = {}
    for field_name in (
        "window_seconds",
        "stuck_draft_seconds",
        "cancel_grace_seconds",
        "review_stale_seconds",
        "on_hold_stale_seconds",
    ):
        raw = payload.get(field_name)
        if raw is None:
            continue
        try:
            kwargs[field_name] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return WatchdogConfig(**kwargs) if kwargs else WatchdogConfig()


def _gather_open_tasks(project_key: str, project_path: Path | None) -> list[Any]:
    """Best-effort load of in-flight tasks for state-based rules.

    Routes through :func:`pollypm.work.create_work_service` (the factory
    landed in #1389), which delegates path resolution to
    :func:`pollypm.work.db_resolver.resolve_work_db_path`. Post-#1004
    the canonical DB lives at ``<workspace_root>/.pollypm/state.db`` —
    a per-project ``<project>/.pollypm/state.db`` is at best stale and
    at worst empty (#1419), so we deliberately do NOT pass a hand-built
    ``db_path`` here. ``project_key`` is forwarded for resolver warnings
    and ``project_path`` for project-aware audit metadata only.

    Powers four rules: ``role_session_missing`` (since #1414), and the
    state-based variants of ``task_review_stale``, ``task_on_hold_stale``,
    and ``stuck_draft`` (#1433). For tasks at the watched states
    (review / on_hold / draft) we re-fetch via :meth:`get` so
    ``transitions`` are hydrated — :meth:`list_nonterminal_tasks` skips
    transition loading for non-plan-review tasks (it only includes
    history for plan-review labelled rows). Without transitions the
    state-based detectors fall back to ``updated_at``, which is touched
    on every status change and is therefore a noisy proxy.

    Returns an empty list on any failure — the watchdog keeps working
    even when the work-service is unreachable; the affected rules
    simply no-op for that project.
    """
    _STATE_RULE_STATES = frozenset({"review", "on_hold", "draft"})
    try:
        from pollypm.work import create_work_service

        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            list_fn = getattr(svc, "list_nonterminal_tasks", None)
            if not callable(list_fn):
                return []
            tasks = list(list_fn(project=project_key))
            # Hydrate transitions for tasks at the watched states so the
            # state-based detectors can compute "time in this state"
            # accurately. Best-effort; if ``svc.get`` blows up we fall
            # through to the un-hydrated row (the detector then falls
            # back to ``updated_at``, which is still better than nothing).
            get_fn = getattr(svc, "get", None)
            if callable(get_fn):
                hydrated: list[Any] = []
                for task in tasks:
                    status = getattr(task, "work_status", None)
                    status_value = getattr(status, "value", status)
                    if status_value in _STATE_RULE_STATES:
                        try:
                            project = getattr(task, "project", "")
                            task_number = getattr(task, "task_number", None)
                            if project and task_number is not None:
                                full = get_fn(f"{project}/{task_number}")
                                if full is not None:
                                    hydrated.append(full)
                                    continue
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "audit.watchdog: hydrate-transitions failed for %s/%s",
                                getattr(task, "project", "?"),
                                getattr(task, "task_number", "?"),
                                exc_info=True,
                            )
                    hydrated.append(task)
                return hydrated
            return tasks
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: open-task load failed for %s",
            project_key, exc_info=True,
        )
        return []


def _gather_storage_windows(storage_closet_name: str | None) -> list[str]:
    """Best-effort list of window names in the storage-closet session.

    Returns an empty list when tmux is unreachable — the
    ``role_session_missing`` rule then conservatively no-ops rather
    than firing false-positives during a tmux outage.
    """
    if not storage_closet_name:
        return []
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
        if not tmux.has_session(storage_closet_name):
            return []
        windows = tmux.list_windows(storage_closet_name)
        return [getattr(w, "name", "") or "" for w in windows]
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: list_windows failed for %s",
            storage_closet_name, exc_info=True,
        )
        return []


def _architect_window_target(
    storage_closet_name: str | None, project_key: str,
) -> str | None:
    """Return ``<closet>:architect-<project>`` if the closet exists.

    The dispatch path send-keys's the brief to that window. Returning
    ``None`` means we can't dispatch right now (no closet, no architect
    window) — the cadence handler treats that as a soft-fail so the
    audit emit + alert still fire.
    """
    if not storage_closet_name or not project_key:
        return None
    return f"{storage_closet_name}:architect-{project_key}"


def _send_brief_to_architect(
    target: str, brief: str,
) -> bool:
    """Best-effort tmux send-keys of the brief to the architect window.

    Returns True on success. The brief is sent as a single text blob
    followed by Enter so the architect agent (Codex/Claude) actually
    processes the turn — agents don't act on their input buffer until
    Enter lands.

    #1420: previously called ``send_keys(..., press_enter=False)`` on
    the rationale that the architect should "review before submitting"
    (mirroring ``_perform_pm_dispatch``'s human-gated semantics). But
    ``_perform_pm_dispatch`` is for *user-initiated* cockpit dispatches
    where a human finalizes the send; here the receiver is an agent,
    so the brief sat in the prompt indefinitely until a human pressed
    Enter — undermining auto-unstick's "no user intervention" framing.

    The Enter is delivered by ``TmuxClient.send_keys`` after a 500ms
    settle delay on the paste-buffer path, mirroring the supervisor
    primer (``cockpit_rail.py`` ``_maybe_prime_*``) and chat dispatch
    convention (#1403, #1404, #1411).
    """
    try:
        from pollypm.tmux.client import TmuxClient

        tmux = TmuxClient()
        # press_enter=True so the agent submits the turn autonomously.
        # See module-level docstring + #1420 for the rationale.
        tmux.send_keys(target, brief, press_enter=True)
        return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: send_keys(%s) failed", target, exc_info=True,
        )
        return False


def _gather_reviewer_evidence(
    *,
    project_key: str,
    project_path: Path | None,
    subject: str,
    msg_store: Any,
    limit_executions: int = 2,
    limit_messages: int = 2,
) -> list[str]:
    """Collect reviewer execution rows + recent inbox messages for ``subject``.

    Returns a list of one-line strings safe to embed verbatim in the
    architect brief. Best-effort — any failure (DB unreachable, message
    store missing) returns an empty list and the brief just says "no
    additional reviewer evidence available". The caller treats the
    evidence as advisory only.

    The execution rows are filtered to the reviewer node (``code_review``
    or any node whose name contains ``review``) so the architect sees
    the rejecting verdict, not the worker's commits. Inbox messages are
    filtered to those that mention the subject so we don't dump the
    whole project queue into the brief.
    """
    evidence: list[str] = []

    # 1. Recent reviewer execution rows from the work-service DB.
    try:
        from pollypm.work import create_work_service

        with create_work_service(
            project_path=project_path,
            project_key=project_key,
        ) as svc:
            try:
                task = svc.get(subject)
            except Exception:  # noqa: BLE001
                task = None
            if task is not None:
                # ``task.executions`` is the in-memory join the work
                # service hydrates on read (see SQLiteWorkService._load_executions).
                executions = list(getattr(task, "executions", None) or [])
                # Filter to reviewer-side rows; sort newest first; cap.
                review_rows = [
                    ex for ex in executions
                    if "review" in (getattr(ex, "node_id", "") or "").lower()
                ]
                review_rows.sort(
                    key=lambda ex: getattr(ex, "completed_at", None)
                    or getattr(ex, "started_at", None)
                    or "",
                    reverse=True,
                )
                for ex in review_rows[:limit_executions]:
                    decision = getattr(ex, "decision", None)
                    decision_value = getattr(decision, "value", decision) or "?"
                    reason = getattr(ex, "decision_reason", None) or ""
                    node = getattr(ex, "node_id", "") or "?"
                    completed = getattr(ex, "completed_at", None)
                    completed_s = (
                        completed.isoformat() if completed is not None else "?"
                    )
                    line = (
                        f"reviewer exec [{node} @ {completed_s}] "
                        f"decision={decision_value}"
                    )
                    if reason:
                        # Truncate aggressively — the brief is
                        # already structured.
                        clipped = reason.strip().splitlines()[0][:240]
                        line = f"{line} reason: {clipped}"
                    evidence.append(line)
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: reviewer-execution fetch failed for %s",
            subject, exc_info=True,
        )

    # 2. Inbox messages that reference the task subject. We use the
    # generic ``query_messages`` surface (post-#349) and filter
    # client-side because every persona persists messages here. The
    # filter is deliberately permissive — anything mentioning the
    # canonical ``project/N`` string is considered relevant.
    try:
        if msg_store is not None and hasattr(msg_store, "query_messages"):
            rows = msg_store.query_messages(project=project_key) or []
            matched: list[dict[str, Any]] = []
            for row in rows:
                body = (
                    str(row.get("body") or "")
                    + " "
                    + str(row.get("title") or "")
                    + " "
                    + str(row.get("subject") or "")
                )
                if subject in body or row.get("subject") == subject:
                    matched.append(row)
            # Sort by created_at desc when available, then cap.
            matched.sort(
                key=lambda r: r.get("created_at") or r.get("ts") or "",
                reverse=True,
            )
            for row in matched[:limit_messages]:
                role = (
                    row.get("requester")
                    or row.get("actor")
                    or row.get("from")
                    or "?"
                )
                title = row.get("title") or row.get("subject") or "(no title)"
                body = (row.get("body") or "").strip().splitlines()
                first_line = body[0][:240] if body else ""
                line = f"inbox msg from {role}: {title}"
                if first_line:
                    line = f"{line} — {first_line}"
                evidence.append(line)
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: reviewer-message fetch failed for %s",
            subject, exc_info=True,
        )

    return evidence


def _enrich_finding_metadata(
    finding: Finding,
    *,
    project_key: str,
    project_path: Path | None,
    msg_store: Any,
) -> Finding:
    """Return a copy of ``finding`` with reviewer evidence folded in.

    Only :data:`RULE_TASK_ON_HOLD_STALE` is enriched today — the brief
    generator surfaces ``metadata['reviewer_evidence']`` for that rule.
    Other rules are returned unchanged. We rebuild a Finding rather than
    mutating because :class:`Finding` is frozen.
    """
    if finding.rule != RULE_TASK_ON_HOLD_STALE:
        return finding
    evidence = _gather_reviewer_evidence(
        project_key=project_key,
        project_path=project_path,
        subject=finding.subject,
        msg_store=msg_store,
    )
    if not evidence:
        return finding
    enriched_meta = {**(finding.metadata or {}), "reviewer_evidence": evidence}
    return Finding(
        rule=finding.rule,
        project=finding.project,
        subject=finding.subject,
        severity=finding.severity,
        message=finding.message,
        recommendation=finding.recommendation,
        metadata=enriched_meta,
    )


def _maybe_dispatch_to_architect(
    finding: Finding,
    *,
    project_path: Path | None,
    storage_closet_name: str | None,
    now: datetime,
) -> str:
    """Apply throttle + emit + send brief. Returns a status code.

    Status codes (used by the per-project counters):
    * ``"skipped"`` — rule isn't dispatchable.
    * ``"throttled"`` — already dispatched within the throttle window.
    * ``"dispatched"`` — brief sent successfully.
    * ``"send_failed"`` — emit ran, send-keys failed.
    """
    if finding.rule not in _DISPATCHABLE_RULES:
        return "skipped"
    if was_recently_dispatched(
        project=finding.project,
        finding_type=finding.rule,
        subject=finding.subject,
        now=now,
        project_path=project_path,
        throttle_seconds=ESCALATION_THROTTLE_SECONDS,
    ):
        return "throttled"
    brief = format_unstick_brief(finding)
    # Emit the dispatch event BEFORE the send so the throttle window
    # is engaged even if the send fails — otherwise a tmux outage would
    # cause every cadence tick to re-emit and re-attempt.
    emit_escalation_dispatched(
        project=finding.project,
        finding_type=finding.rule,
        subject=finding.subject,
        brief=brief,
        project_path=project_path,
    )
    target = _architect_window_target(storage_closet_name, finding.project)
    if target is None:
        return "send_failed"
    if not _send_brief_to_architect(target, brief):
        return "send_failed"
    return "dispatched"


def _scan_one_project(
    *,
    project_key: str,
    project_path: Path | None,
    msg_store: Any,
    state_store: Any,
    now: datetime,
    config: WatchdogConfig,
    storage_closet_name: str | None = None,
) -> dict[str, int]:
    """Scan one project and route every finding. Returns counters."""
    counters = {
        "findings": 0,
        "alerts_raised": 0,
        "alert_failures": 0,
        "dispatches_sent": 0,
        "dispatches_throttled": 0,
        "dispatches_failed": 0,
    }
    open_tasks = _gather_open_tasks(project_key, project_path)
    storage_window_names = _gather_storage_windows(storage_closet_name)
    try:
        findings = scan_project(
            project_key,
            project_path=project_path,
            now=now,
            config=config,
            open_tasks=open_tasks,
            storage_window_names=storage_window_names,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "audit.watchdog: scan_project(%s) failed",
            project_key, exc_info=True,
        )
        return counters

    for finding in findings:
        counters["findings"] += 1
        emit_finding(finding)
        if _route_to_alert_sink(
            finding, msg_store=msg_store, state_store=state_store,
        ):
            counters["alerts_raised"] += 1
        else:
            counters["alert_failures"] += 1
            # Last-resort surface so a finding with no alert sink
            # still lands somewhere visible to operators.
            logger.warning(
                "audit.watchdog finding [%s] %s/%s: %s | %s",
                finding.rule, finding.project, finding.subject,
                finding.message, finding.recommendation,
            )
        # #1414 — eligible findings get an architect dispatch on top
        # of the alert. Throttle window is owned by the audit log so
        # repeat dispatches are deduped across cadence-process restarts.
        # #1424 — for ``task_on_hold_stale`` we enrich the finding with
        # the reviewer's recent execution rows + inbox messages so the
        # architect's brief carries the rejection rationale, not just
        # the bare on_hold timestamp.
        dispatch_finding = _enrich_finding_metadata(
            finding,
            project_key=project_key,
            project_path=project_path,
            msg_store=msg_store,
        )
        outcome = _maybe_dispatch_to_architect(
            dispatch_finding,
            project_path=project_path,
            storage_closet_name=storage_closet_name,
            now=now,
        )
        if outcome == "dispatched":
            counters["dispatches_sent"] += 1
        elif outcome == "throttled":
            counters["dispatches_throttled"] += 1
        elif outcome == "send_failed":
            counters["dispatches_failed"] += 1
    return counters


def audit_watchdog_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """Cadence handler entry point.

    Reads the audit log for every known project, runs the watchdog
    detectors, and routes every finding to an alert + a forensic
    audit event.

    Returns a dict with per-project + total counters for observability.
    """
    from pollypm.runtime_services import load_runtime_services

    config = _config_from_payload(payload or {})
    config_path_hint = (
        payload.get("config_path") if isinstance(payload, dict) else None
    )
    config_path = Path(config_path_hint) if config_path_hint else None

    services = load_runtime_services(config_path=config_path)
    now = datetime.now(UTC)

    # Liveness ping — emitted before scanning so that even if
    # scanning blows up the heartbeat-tick still lands in the log.
    emit_heartbeat_tick(metadata={"cadence": AUDIT_WATCHDOG_SCHEDULE})

    totals = {
        "projects_scanned": 0,
        "findings": 0,
        "alerts_raised": 0,
        "alert_failures": 0,
        "dispatches_sent": 0,
        "dispatches_throttled": 0,
        "dispatches_failed": 0,
    }

    try:
        seen_projects: set[str] = set()
        storage_closet_name = getattr(
            services, "storage_closet_name", None,
        )
        for project in services.known_projects or ():
            project_key = getattr(project, "key", None)
            if not project_key or project_key in seen_projects:
                continue
            seen_projects.add(project_key)
            project_path = getattr(project, "path", None)
            partial = _scan_one_project(
                project_key=project_key,
                project_path=Path(project_path) if project_path else None,
                msg_store=services.msg_store,
                state_store=services.state_store,
                now=now,
                config=config,
                storage_closet_name=storage_closet_name,
            )
            totals["projects_scanned"] += 1
            for k, v in partial.items():
                totals[k] += v
    finally:
        try:
            services.close()
        except Exception:  # noqa: BLE001
            logger.debug(
                "audit.watchdog: services.close raised", exc_info=True,
            )

    return {"outcome": "swept", **totals}
