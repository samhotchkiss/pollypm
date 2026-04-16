from __future__ import annotations

import re

from pollypm.heartbeat_loop import (
    SessionHealth,
    SessionSignals,
    classify_session_health,
    select_intervention,
)
from pollypm.heartbeats.base import HeartbeatBackend, HeartbeatSessionContext


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
                    api.record_event(
                        context.session_name,
                        "heartbeat_error",
                        f"Error processing session: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    pass
        api.record_event(
            "heartbeat",
            "heartbeat",
            f"Heartbeat sweep completed with {len(api.open_alerts())} open alerts",
        )
        return api.open_alerts()

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
                api.record_event("heartbeat", "unmanaged_window", message)
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
                health = classify_session_health(signals)
                runtime = api.supervisor.store.get_session_runtime(context.session_name)
                prev = runtime.recovery_attempts if runtime else 0
                intervention = select_intervention(health, signals, previous_interventions=prev)
                if intervention and context.role == "worker":
                    # Use Haiku to decide the right action for idle workers.
                    # The LLM reads the snapshot and classifies: push forward,
                    # nudge, do nothing, or escalate.
                    self._triage_stalled_worker(api, context)
                elif intervention and intervention.action == "escalate":
                    self._escalate(api, context, intervention.reason)
            except Exception:  # noqa: BLE001
                pass

    def _escalate(self, api, context: HeartbeatSessionContext, reason: str) -> None:
        """Raise a durable alert so the user sees a stuck session in the cockpit."""
        # Dedup: don't re-escalate if we escalated this session within 10 minutes
        try:
            from datetime import UTC, datetime
            last = api.supervisor.store.last_event_at(context.session_name, "escalated")
            if last:
                age = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
                if age < 600:
                    return
        except Exception:  # noqa: BLE001
            pass

        try:
            api.raise_alert(
                context.session_name,
                "stuck_session",
                "warn",
                f"{context.session_name} needs attention: {reason[:160]}",
            )
            api.supervisor.store.record_event(
                context.session_name, "escalated",
                f"Raised stuck_session alert: {reason[:80]}",
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
            recent = api.supervisor.store.recent_events(limit=200)
            now = datetime.now(UTC)
            # Debounce: skip if worker received input recently
            for event in recent:
                if (
                    event.session_name == context.session_name
                    and event.event_type == "send_input"
                    and (now - datetime.fromisoformat(event.created_at)).total_seconds() < 120
                ):
                    return
            # Rate limit + circuit breaker
            nudge_count = 0
            most_recent_age = None
            for event in recent:
                if event.session_name != context.session_name or event.event_type != "nudge":
                    continue
                age = (now - datetime.fromisoformat(event.created_at)).total_seconds()
                if age < 3600:
                    nudge_count += 1
                if most_recent_age is None:
                    most_recent_age = age
            # Circuit breaker: too many nudges → recover the worker
            if nudge_count >= self._MAX_NUDGES_BEFORE_RECOVERY:
                api.recover_session(
                    context.session_name,
                    failure_type="unresponsive",
                    message=f"Worker unresponsive after {nudge_count} nudges — restarting",
                )
                return
            # Rate limit
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
            api.supervisor.store.record_event(context.session_name, "nudge", f"Sent nudge: {message[:80]}")
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
            api.supervisor.store.record_event(
                context.session_name, "heuristic_triage", "Pushed forward: proceed signal detected",
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
        text = (context.transcript_delta or context.pane_text or "").strip()
        if not text:
            return "unclear", "No new transcript or pane output to classify"
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
