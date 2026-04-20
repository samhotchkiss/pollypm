from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pollypm.heartbeats.base import HeartbeatBackend, HeartbeatSessionContext
from pollypm.recovery.base import (
    InterventionHistoryEntry,
    SessionHealth,
    SessionSignals,
)
from pollypm.recovery.default import DefaultRecoveryPolicy

logger = logging.getLogger(__name__)


def _collect_work_service_signals(
    api: Any, context: HeartbeatSessionContext,
) -> dict[str, Any]:
    """Populate work-service-aware signal fields for ``context``.

    Queries (#249):
      * ``work_sessions`` — active claim (task_id + claim timestamp).
      * ``events`` — last event tied to this session.
      * ``git log -1 --format=%ct`` — last commit on the worktree.

    Returns a dict with ``active_claim_task_id``, ``claim_age_seconds``,
    ``last_event_seconds_ago``, ``last_commit_seconds_ago`` — any key may
    be absent (or value ``None``) when the underlying probe fails.
    Exceptions are swallowed — the classifier degrades to mechanical mode.
    """
    out: dict[str, Any] = {
        "active_claim_task_id": None,
        "claim_age_seconds": None,
        "last_event_seconds_ago": None,
        "last_commit_seconds_ago": None,
    }

    now = datetime.now(timezone.utc)

    # -- last event tied to this session from the unified message store --
    try:
        rows = api.supervisor.msg_store.query_messages(
            type="event",
            scope=context.session_name,
            limit=1,
        )
        if rows:
            created_at = rows[0].get("created_at")
            if created_at is None:
                raise ValueError("event row missing created_at")
            stamp = (
                created_at.isoformat()
                if hasattr(created_at, "isoformat")
                else str(created_at)
            )
            ts = datetime.fromisoformat(stamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            out["last_event_seconds_ago"] = int((now - ts).total_seconds())
    except Exception:  # noqa: BLE001
        logger.debug(
            "work-signal: last_event probe failed for %s",
            context.session_name, exc_info=True,
        )

    # -- active claim + commit age via the work service -----------------
    # The work service lives in ``<project_path>/.pollypm/state.db``.
    # Not every session maps to a project with a work DB; tests and
    # non-work-service setups skip through cleanly.
    try:
        config = api.supervisor.config
        session_cfg = config.sessions.get(context.session_name)
        if session_cfg is None:
            return out
        project_cfg = config.projects.get(session_cfg.project)
        if project_cfg is None:
            return out
        project_path: Path = project_cfg.path
        work_db = project_path / ".pollypm" / "state.db"
        if not work_db.exists():
            return out

        from pollypm.work.sqlite_service import SQLiteWorkService

        worktree_path: str | None = None
        claim_started_at: str | None = None
        claim_task_id: str | None = None
        try:
            with SQLiteWorkService(
                db_path=work_db, project_path=project_path,
            ) as svc:
                sessions = svc.list_worker_sessions(
                    project=session_cfg.project, active_only=True,
                )
                # The caller's session name may be ``worker-<proj>`` while
                # ``agent_name`` is typically ``worker``. The simplest
                # reliable correlation is: an in-progress task whose
                # ``work_sessions`` row has the most recent started_at
                # wins. In practice worker sessions are per-task (see
                # #239-ish) so there's usually only one active row.
                best = None
                for row in sessions:
                    if best is None or (row.started_at or "") > (best.started_at or ""):
                        best = row
                if best is not None:
                    claim_started_at = best.started_at
                    claim_task_id = f"{best.task_project}/{best.task_number}"
                    worktree_path = best.worktree_path
        except Exception:  # noqa: BLE001
            logger.debug(
                "work-signal: work service query failed for %s",
                context.session_name, exc_info=True,
            )

        if claim_started_at and claim_task_id:
            try:
                ts = datetime.fromisoformat(claim_started_at)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                out["active_claim_task_id"] = claim_task_id
                out["claim_age_seconds"] = int((now - ts).total_seconds())
            except Exception:  # noqa: BLE001
                pass

        # Git commit timestamp on the claimed task's worktree.
        if worktree_path:
            try:
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%ct"],
                    cwd=worktree_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    ct = int(result.stdout.strip())
                    out["last_commit_seconds_ago"] = int(
                        now.timestamp() - ct,
                    )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "work-signal: git log failed for %s",
                    worktree_path, exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.debug(
            "work-signal: outer probe failed for %s",
            context.session_name, exc_info=True,
        )

    return out

_DEFAULT_POLICY = DefaultRecoveryPolicy()


def _classify_session_health(signals: SessionSignals) -> SessionHealth:
    return _DEFAULT_POLICY.classify(signals)


def _select_intervention(
    health: SessionHealth,
    signals: SessionSignals,
    *,
    previous_interventions: int = 0,
):
    history = [
        InterventionHistoryEntry(action="") for _ in range(previous_interventions)
    ]
    return _DEFAULT_POLICY.select_intervention(health, signals, history)


class LocalHeartbeatBackend(HeartbeatBackend):
    name = "local"
    _UNMANAGED_WINDOW_ALERT_PREFIX = "unmanaged_window:"

    _AUTH_FAILURE_PATTERNS = (
        "authentication failure",
        "not authenticated",
        "login required",
        "please login",
        "please log in",
        "invalid api key",
    )
    _WAITING_PATTERNS = (
        "let me know",
        "waiting for your",
        "please choose",
        "confirm ",
        "which would you like",
        "need your input",
        "need user input",
        "approve",
    )
    _DONE_PATTERNS = (
        "task complete",
        "completed",
        "all tests passed",
        "done.",
        "ready for review",
        "finished",
        "implemented",
        "resolved",
    )
    _FOLLOWUP_PATTERNS = (
        "next step",
        "next,",
        "remaining",
        "still need",
        "todo",
        "to do",
        "follow up",
        "continue with",
        "not finished",
        "partial",
    )

    def run(self, api, *, snapshot_lines: int = 200):
        self._process_unmanaged_windows(api)
        for context in api.list_sessions():
            try:
                self._process_session(api, context)
            except Exception as exc:  # noqa: BLE001
                # Log and continue — don't let one session abort the entire sweep
                try:
                    from pollypm.plugins_builtin.activity_feed.summaries import (
                        activity_summary,
                    )

                    api.record_event(
                        context.session_name,
                        "heartbeat_error",
                        activity_summary(
                            summary=f"Error processing session: {exc}",
                            severity="critical",
                            verb="errored",
                            subject=context.session_name,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        open_alerts = api.open_alerts()
        api.record_event(
            "heartbeat",
            "heartbeat",
            activity_summary(
                summary=f"Heartbeat sweep completed with {len(open_alerts)} open alerts",
                severity="recommendation" if open_alerts else "routine",
                verb="swept",
                subject="heartbeat",
                open_alerts=len(open_alerts),
            ),
        )
        return open_alerts

    def _process_unmanaged_windows(self, api) -> None:
        current_alert_types: set[str] = set()
        existing_alert_types = {
            alert.alert_type
            for alert in api.open_alerts()
            if alert.session_name == "heartbeat" and alert.alert_type.startswith(self._UNMANAGED_WINDOW_ALERT_PREFIX)
        }
        for window in api.list_unmanaged_windows():
            alert_type = f"{self._UNMANAGED_WINDOW_ALERT_PREFIX}{window.tmux_session}:{window.window_name}"
            current_alert_types.add(alert_type)
            message = (
                f"Found unmanaged tmux window {window.window_name} in session {window.tmux_session} "
                f"running {window.pane_command}"
            )
            api.raise_alert("heartbeat", alert_type, "warn", message)
            if alert_type not in existing_alert_types:
                from pollypm.plugins_builtin.activity_feed.summaries import (
                    activity_summary,
                )

                api.record_event(
                    "heartbeat",
                    "unmanaged_window",
                    activity_summary(
                        summary=message,
                        severity="recommendation",
                        verb="unmanaged",
                        subject=window.window_name,
                    ),
                )
        for alert_type in existing_alert_types - current_alert_types:
            api.clear_alert("heartbeat", alert_type)

    def _process_session(self, api, context: HeartbeatSessionContext) -> None:
        alerts: list[str] = []
        # Skip disabled sessions (decommissioned via pm worker-stop)
        # Also skip if the operator recently managed workers (avoid race with Polly)
        try:
            rt = api.supervisor.store.get_session_runtime(context.session_name)
            if rt and rt.status in ("disabled", "switching"):
                return
        except (AttributeError, Exception):  # noqa: BLE001
            pass  # API may not have supervisor (e.g., tests)
        mechanical_only = context.role == "heartbeat-supervisor"
        if not context.window_present:
            api.raise_alert(
                context.session_name,
                "missing_window",
                "error",
                f"Expected tmux window {context.window_name} in session {context.tmux_session}",
            )
            api.set_session_status(context.session_name, "recovering", reason="Expected tmux window is missing")
            api.recover_session(context.session_name, failure_type="missing_window", message="Expected tmux window is missing")
            api.update_cursor(
                context.session_name,
                source_path=context.source_path,
                last_offset=context.source_bytes,
                verdict="missing_window",
                reason="Expected tmux window is missing",
            )
            return

        api.record_observation(context)
        api.clear_alert(context.session_name, "missing_window")

        if context.pane_dead:
            api.raise_alert(
                context.session_name,
                "pane_dead",
                "error",
                f"Pane {context.pane_id} in window {context.window_name} has exited",
            )
            api.set_session_status(context.session_name, "recovering", reason="Pane exited")
            api.recover_session(context.session_name, failure_type="pane_dead", message="Pane exited")
            alerts.append("pane_dead")
        else:
            api.clear_alert(context.session_name, "pane_dead")

        if (context.pane_command or "") in {"bash", "zsh", "sh", "fish"}:
            api.raise_alert(
                context.session_name,
                "shell_returned",
                "warn",
                f"Window {context.window_name} appears to be back at the shell prompt ({context.pane_command})",
            )
            alerts.append("shell_returned")
        else:
            api.clear_alert(context.session_name, "shell_returned")

        # Sessions parked at a prompt are legitimately idle — not an alert condition.
        # Only the suspected_loop detector (below) alerts on sustained identical snapshots.
        api.clear_alert(context.session_name, "idle_output")

        if not mechanical_only and context.previous_snapshot_hash and context.previous_snapshot_hash == context.snapshot_hash:
            hashes = api.recent_snapshot_hashes(context.session_name, limit=3)
            if len(hashes) == 3 and len(set(hashes)) == 1:
                # Check if this worker has any assigned work — if not, it's
                # legitimately idle and shouldn't be flagged.
                if context.role == "worker" and not self._has_pending_work(api, context):
                    api.clear_alert(context.session_name, "suspected_loop")
                else:
                    api.raise_alert(
                        context.session_name,
                        "suspected_loop",
                        "warn",
                        f"Window {context.window_name} has produced effectively the same snapshot for 3 heartbeats",
                    )
                    alerts.append("suspected_loop")
                    # After 5 consecutive identical snapshots, queue a Haiku triage
                    longer_hashes = api.recent_snapshot_hashes(context.session_name, limit=5)
                    if len(longer_hashes) == 5 and len(set(longer_hashes)) == 1:
                        if context.role == "worker":
                            self._triage_stalled_worker(api, context)
            else:
                api.clear_alert(context.session_name, "suspected_loop")
        else:
            api.clear_alert(context.session_name, "suspected_loop")

        combined_text = "\n".join(part for part in [context.transcript_delta, context.pane_text] if part).lower()
        status_locked = False
        if any(pattern in combined_text for pattern in self._AUTH_FAILURE_PATTERNS):
            api.raise_alert(
                context.session_name,
                "auth_broken",
                "error",
                f"Window {context.window_name} reported authentication failure",
            )
            api.mark_account_auth_broken(
                context.account_name,
                context.provider,
                reason="live session reported authentication failure",
            )
            api.set_session_status(context.session_name, "auth_broken", reason="Authentication failure reported")
            alerts.append("auth_broken")
            status_locked = True
        else:
            api.clear_alert(context.session_name, "auth_broken")

        if context.pane_dead:
            status_locked = True

        if mechanical_only:
            verdict, reason = ("healthy", "Heartbeat supervisor only checks mechanical session health")
            api.clear_alert(context.session_name, "needs_followup")
            if not status_locked:
                api.set_session_status(context.session_name, "healthy", reason=reason)
        else:
            verdict, reason = self._classify(context)
            if verdict == "needs_followup":
                api.raise_alert(context.session_name, "needs_followup", "warn", reason)
                if not status_locked:
                    api.set_session_status(context.session_name, "needs_followup", reason=reason)
                # Alerts are visible in the cockpit and via `pm alerts`.
                # No need to inject messages into the operator chat —
                # the operator gets nudged only when *it* is stalled.
                alerts.append("needs_followup")
            else:
                api.clear_alert(context.session_name, "needs_followup")
                if not status_locked:
                    if verdict == "blocked":
                        api.set_session_status(context.session_name, "waiting_on_user", reason=reason)
                    elif verdict == "done":
                        api.set_session_status(context.session_name, "idle", reason=reason)
                    else:
                        api.set_session_status(context.session_name, "healthy", reason=reason)

        api.record_checkpoint(context, alerts=alerts)
        api.update_cursor(
            context.session_name,
            source_path=context.source_path,
            last_offset=context.source_bytes,
            snapshot_hash=context.snapshot_hash,
            verdict=verdict,
            reason=reason,
        )

        # Use the structured classification engine for intervention decisions
        if not mechanical_only:
            try:
                signals = self._context_to_signals(context, api)
                health = _classify_session_health(signals)
                runtime = api.supervisor.store.get_session_runtime(context.session_name)
                prev = runtime.recovery_attempts if runtime else 0
                intervention = _select_intervention(health, signals, previous_interventions=prev)
                # #249 — work-aware interventions. These dispatch before
                # the generic worker-triage path so the policy-chosen
                # action actually runs.
                if intervention and intervention.action == "resume_ping":
                    self._apply_resume_ping(api, context, signals, intervention)
                elif intervention and intervention.action == "prompt_pm_task_next":
                    self._apply_prompt_pm_task_next(api, context)
                elif intervention and context.role == "worker":
                    # Use Haiku to decide the right action for idle workers.
                    # The LLM reads the snapshot and classifies: push forward,
                    # nudge, do nothing, or escalate.
                    self._triage_stalled_worker(api, context)
                elif intervention and intervention.action == "escalate":
                    self._escalate(api, context, intervention.reason)
            except Exception:  # noqa: BLE001
                pass

    def _apply_resume_ping(
        self, api, context: HeartbeatSessionContext, signals: SessionSignals,
        intervention,
    ) -> None:
        """Emit a resume ping via task_assignment_notify (#249).

        Reuses the existing 30-min dedupe via ``notify()``. Best-effort —
        all failures are logged and swallowed so the sweep never aborts
        on an apply hiccup.
        """
        try:
            from pollypm.plugins_builtin.task_assignment_notify.handlers.sweep import (
                _build_event_for_task,
            )
            from pollypm.plugins_builtin.task_assignment_notify.resolver import (
                DEDUPE_WINDOW_SECONDS,
                load_runtime_services,
                notify as _notify,
            )
            from pollypm.work.sqlite_service import SQLiteWorkService

            task_id = signals.active_claim_task_id
            if not task_id or "/" not in task_id:
                return
            project, number_s = task_id.rsplit("/", 1)
            try:
                task_number = int(number_s)
            except ValueError:
                return

            config = api.supervisor.config
            project_cfg = config.projects.get(project)
            if project_cfg is None:
                return
            work_db = project_cfg.path / ".pollypm" / "state.db"
            if not work_db.exists():
                return

            services = load_runtime_services()
            try:
                with SQLiteWorkService(
                    db_path=work_db, project_path=project_cfg.path,
                ) as svc:
                    tasks = svc.list_tasks(project=project)
                    task = next(
                        (
                            t for t in tasks
                            if t.task_number == task_number
                        ),
                        None,
                    )
                    if task is None:
                        return
                    event = _build_event_for_task(svc, task)
                if event is None:
                    return
                _notify(
                    event, services=services,
                    throttle_seconds=DEDUPE_WINDOW_SECONDS,
                )
            finally:
                closer = getattr(services.work_service, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001
                        pass

            # Raise a low-severity alert so the cockpit surfaces it.
            try:
                api.supervisor.msg_store.upsert_alert(
                    context.session_name,
                    f"stuck_on_task:{task_id}",
                    "warning",
                    f"Stuck on {task_id}: {intervention.reason[:140]}",
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "resume_ping apply failed for %s",
                context.session_name, exc_info=True,
            )

    def _apply_prompt_pm_task_next(
        self, api, context: HeartbeatSessionContext,
    ) -> None:
        """Send `pm task next` into a silent worker session (#249).

        The send is routed through the same rate-limit path used for
        ordinary nudges so we don't spam a worker that's slow to pick up.
        """
        try:
            api.send_session_message(
                context.session_name,
                "pm task next",
                owner="heartbeat",
            )
            try:
                from pollypm.plugins_builtin.activity_feed.summaries import (
                    activity_summary,
                )

                api.supervisor.msg_store.append_event(
                    scope=context.session_name,
                    sender=context.session_name,
                    subject="silent_worker_prompt",
                    payload={
                        "message": activity_summary(
                            summary="Sent 'pm task next' to silent worker",
                            severity="recommendation",
                            verb="prompted",
                            subject=context.session_name,
                        ),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "prompt_pm_task_next apply failed for %s",
                context.session_name, exc_info=True,
            )

    def _escalate(self, api, context: HeartbeatSessionContext, reason: str) -> None:
        """Raise a durable alert so the user sees a stuck session in the cockpit."""
        # Dedup: don't re-escalate if we escalated this session within 10 minutes.
        # #349: escalation events now live on the unified ``messages`` table.
        try:
            from datetime import UTC, datetime
            recent = api.supervisor.msg_store.query_messages(
                type="event",
                scope=context.session_name,
                limit=20,
            )
            now = datetime.now(UTC)
            for event in recent:
                if event.get("subject") != "escalated":
                    continue
                created_at = event.get("created_at")
                if created_at is None:
                    continue
                stamp = (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else str(created_at)
                )
                try:
                    parsed = datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                age = (now - parsed).total_seconds()
                if age < 600:
                    return
                break
        except Exception:  # noqa: BLE001
            pass

        try:
            api.raise_alert(
                context.session_name,
                "stuck_session",
                "warn",
                f"{context.session_name} needs attention: {reason[:160]}",
            )
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            api.supervisor.msg_store.append_event(
                scope=context.session_name,
                sender=context.session_name,
                subject="escalated",
                payload={
                    "message": activity_summary(
                        summary=f"Raised stuck_session alert: {reason[:80]}",
                        severity="critical",
                        verb="escalated",
                        subject=context.session_name,
                    ),
                    "reason": reason,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _context_to_signals(self, context: HeartbeatSessionContext, api) -> SessionSignals:
        """Bridge HeartbeatSessionContext to SessionSignals for the classification engine."""
        hashes = api.recent_snapshot_hashes(context.session_name, limit=5)
        repeated = 0
        if hashes:
            for h in hashes:
                if h == hashes[0]:
                    repeated += 1
                else:
                    break

        # Work-service-aware signals (#249). Populate best-effort — if
        # the work service can't be reached (tests, missing DB, etc.)
        # these stay None and the classifier falls through to the
        # pre-existing mechanical ladder.
        work_signals = _collect_work_service_signals(api, context)

        return SessionSignals(
            session_name=context.session_name,
            window_present=context.window_present,
            pane_dead=context.pane_dead,
            output_stale=not bool(context.transcript_delta),
            snapshot_repeated=repeated,
            auth_failure=any(
                p in (context.transcript_delta or context.pane_text or "").lower()
                for p in self._AUTH_FAILURE_PATTERNS
            ),
            has_transcript_delta=bool(context.transcript_delta),
            last_verdict=context.cursor.last_verdict if context.cursor else "",
            idle_cycles=repeated,
            session_role=context.role or "",
            turn_active=bool(context.transcript_delta),
            active_claim_task_id=work_signals.get("active_claim_task_id"),
            claim_age_seconds=work_signals.get("claim_age_seconds"),
            last_event_seconds_ago=work_signals.get("last_event_seconds_ago"),
            last_commit_seconds_ago=work_signals.get("last_commit_seconds_ago"),
        )

    # Rate limit nudges: max once per session per 10 minutes
    # Rate limits for worker nudges
    _NUDGE_COOLDOWN_SECONDS = 600
    _MAX_NUDGES_BEFORE_RECOVERY = 6

    def _nudge_stalled_worker(self, api, context: HeartbeatSessionContext) -> None:
        """Send a targeted nudge to a stalled WORKER. Never targets the operator."""
        if context.role != "worker":
            return
        # Don't nudge workers with no pending work — they're legitimately idle
        if not self._has_pending_work(api, context):
            return
        try:
            from datetime import UTC, datetime
            # #349: events migrated to the unified ``messages`` table.
            recent = api.supervisor.msg_store.query_messages(
                type="event",
                scope=context.session_name,
                limit=200,
            )
            now = datetime.now(UTC)

            def _age_seconds(event: dict) -> float | None:
                created_at = event.get("created_at")
                if created_at is None:
                    return None
                stamp = (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else str(created_at)
                )
                try:
                    parsed = datetime.fromisoformat(stamp)
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return (now - parsed).total_seconds()

            # Debounce: skip if worker received input recently.
            for event in recent:
                if event.get("subject") != "send_input":
                    continue
                age = _age_seconds(event)
                if age is not None and age < 120:
                    return
            # Rate limit + circuit breaker.
            nudge_count = 0
            most_recent_age: float | None = None
            for event in recent:
                if event.get("subject") != "nudge":
                    continue
                age = _age_seconds(event)
                if age is None:
                    continue
                if age < 3600:
                    nudge_count += 1
                if most_recent_age is None:
                    most_recent_age = age
            # Circuit breaker: too many nudges → recover the worker.
            if nudge_count >= self._MAX_NUDGES_BEFORE_RECOVERY:
                api.recover_session(
                    context.session_name,
                    failure_type="unresponsive",
                    message=f"Worker unresponsive after {nudge_count} nudges — restarting",
                )
                return
            # Rate limit.
            if most_recent_age is not None and most_recent_age < self._NUDGE_COOLDOWN_SECONDS:
                return
        except Exception:  # noqa: BLE001
            pass
        # Context-aware nudge for the worker
        snippet = (context.pane_text or "").strip().splitlines()[-1][:80] if context.pane_text else ""
        if "permission" in snippet.lower() or "approve" in snippet.lower():
            message = "You appear stuck on a permissions prompt. Accept or work around it."
        elif "error" in snippet.lower() or "failed" in snippet.lower():
            message = "You hit an error. Read it carefully, fix the root cause, and continue."
        else:
            message = "State the remaining task in one sentence, execute the next step, and report."
        api.send_session_message(context.session_name, message, owner="heartbeat")
        try:
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            api.supervisor.msg_store.append_event(
                scope=context.session_name,
                sender=context.session_name,
                subject="nudge",
                payload={
                    "message": activity_summary(
                        summary=f"Sent nudge: {message[:80]}",
                        severity="recommendation",
                        verb="nudged",
                        subject=context.session_name,
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    def _has_pending_work(self, api, context: HeartbeatSessionContext) -> bool:
        """Check if a worker's project has ready/in-progress tasks."""
        try:
            config = api.supervisor.config
            session = config.sessions.get(context.session_name)
            if session is None:
                return False
            project = config.projects.get(session.project)
            if project is None:
                return False
            # Check issue tracker
            from pollypm.task_backends import get_task_backend
            backend = get_task_backend(project.path)
            if backend.exists():
                tasks = backend.list_tasks(states=["01-ready", "02-in-progress"])
                if tasks:
                    return True
            # Check the work service for non-terminal tasks assigned to this
            # worker's project.
            try:
                from pollypm.work.sqlite_service import SQLiteWorkService

                db_path = project.path / ".pollypm" / "state.db"
                if db_path.exists():
                    with SQLiteWorkService(
                        db_path=db_path, project_path=project.path,
                    ) as svc:
                        tasks = svc.list_tasks(project=session.project)
                    for t in tasks:
                        if t.work_status.value not in ("done", "cancelled"):
                            return True
            except Exception:  # noqa: BLE001
                pass
            return False
        except Exception:  # noqa: BLE001
            return True  # Assume work exists on error — don't falsely recover idle workers

    def _triage_stalled_worker(self, api, context: HeartbeatSessionContext) -> None:
        """Fast heuristic triage for stalled workers (60s path).

        No Haiku call — the 5-minute sweep handles LLM analysis.
        This is just pattern matching on the snapshot for quick decisions.
        """
        snapshot = (context.pane_text or "").strip()
        if not snapshot:
            return

        lowered = snapshot.lower()

        # Worker asking for permission → push forward
        proceed_signals = ["if you want", "shall i", "should i", "want me to", "i can do"]
        if any(sig in lowered for sig in proceed_signals):
            api.send_session_message(
                context.session_name,
                "Yes, proceed. Do the next step you outlined.",
                owner="heartbeat",
            )
            from pollypm.plugins_builtin.activity_feed.summaries import (
                activity_summary,
            )

            api.supervisor.msg_store.append_event(
                scope=context.session_name,
                sender=context.session_name,
                subject="heuristic_triage",
                payload={
                    "message": activity_summary(
                        summary="Pushed forward: proceed signal detected",
                        severity="routine",
                        verb="triaged",
                        subject=context.session_name,
                    ),
                },
            )
            return

        # Worker has obvious next steps
        next_step_signals = ["next step", "next,", "todo", "remaining", "need to"]
        if any(sig in lowered for sig in next_step_signals):
            self._nudge_stalled_worker(api, context)
            return

        # Worker hit an error
        if "error" in lowered or "failed" in lowered or "traceback" in lowered:
            self._nudge_stalled_worker(api, context)
            return

    def _classify(self, context: HeartbeatSessionContext) -> tuple[str, str]:
        # Only classify on NEW transcript content. Falling back to the
        # full pane_text for idle sessions means old "remaining" /
        # "next" language stays matched forever, continuously firing
        # ``needs_followup`` alerts against agents that are actually
        # just waiting for the next user prompt. The heartbeat loop
        # already emits a separate ``suspected_loop`` signal for true
        # stuck sessions; classify should only speak about fresh work.
        delta = (context.transcript_delta or "").strip()
        if not delta:
            return "unclear", "No new transcript output since last heartbeat"
        text = delta
        # Extract a useful snippet from the tail of the text for the reason
        snippet = text.strip().splitlines()[-1][:120] if text.strip() else ""
        lowered = text.lower()
        if any(pattern in lowered for pattern in self._WAITING_PATTERNS):
            return "blocked", f"Waiting on operator input — {snippet}"
        if any(pattern in lowered for pattern in self._FOLLOWUP_PATTERNS):
            return "needs_followup", f"Additional work remains — {snippet}"
        if any(pattern in lowered for pattern in self._DONE_PATTERNS):
            return "done", f"Last turn appears complete — {snippet}"
        if re.search(r"\b(next|remaining|follow-up|follow up|still need)\b", lowered):
            return "needs_followup", f"Additional work remains — {snippet}"
        if lowered.endswith("?"):
            return "blocked", f"Last turn ended with a question — {snippet}"
        return "unclear", f"Could not confidently classify the last turn — {snippet}"
