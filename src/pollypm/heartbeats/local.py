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
        # Inbox delivery is handled by the async inbox_delivery job in Phase 3
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

        # Detect worker completion — check on every sweep for workers showing
        # identical snapshots (at a prompt, not actively working).
        if not mechanical_only and context.role == "worker":
            hashes = api.recent_snapshot_hashes(context.session_name, limit=2)
            if len(hashes) >= 2 and hashes[0] == hashes[1]:
                self._detect_worker_completion(api, context)

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
                    self._escalate_to_inbox(api, context, intervention.reason)
            except Exception:  # noqa: BLE001
                pass

    def _escalate_to_inbox(self, api, context: HeartbeatSessionContext, reason: str) -> None:
        """Escalate a stuck session — activate the triage session if available, otherwise inbox."""
        # Try triage session first — it can analyze and act without cluttering the main session
        if self._activate_triage(api, context, reason):
            return
        # Fall back to inbox — send to Polly first, not the user.
        # Polly can try to resolve it; only escalate to user if Polly can't.
        try:
            from pollypm.inbox_v2 import create_message
            snippet = (context.pane_text or "").strip()[-200:] if context.pane_text else "no snapshot available"
            create_message(
                api.supervisor.config.project.root_dir,
                sender="heartbeat",
                subject=f"[Escalation] {context.session_name} needs attention: {reason[:60]}",
                to="polly",
                owner="polly",
                body=(
                    f"Session '{context.session_name}' is stuck and automated intervention hasn't helped.\n"
                    f"\n"
                    f"Reason: {reason}\n"
                    f"Role: {context.role}\n"
                    f"Window: {context.window_name}\n"
                    f"\n"
                    f"Last snapshot:\n"
                    f"```\n{snippet}\n```\n"
                    f"\n"
                    f"Try to resolve this yourself first:\n"
                    f"  pm send {context.session_name} \"<instructions>\"\n"
                    f"  pm worker-start {context.session_name.replace('worker_', '', 1)}  (restart)\n"
                    f"\n"
                    f"Only escalate to the user with `pm notify` if you cannot resolve it.\n"
                ),
            )
            api.supervisor.store.record_event(
                context.session_name, "escalated",
                f"Escalated to inbox: {reason[:80]}",
            )
        except Exception:  # noqa: BLE001
            pass

    def _activate_triage(self, api, context: HeartbeatSessionContext, reason: str) -> bool:
        """Activate a triage session to handle an issue.

        Auto-creates and launches the triage session if needed. Delivers the
        briefing via inbox so it's tracked and doesn't pollute the session
        with raw pasted text.
        """
        try:
            from pollypm.agent_profiles.builtin import triage_prompt
            from pollypm.config import write_config
            from pollypm.inbox_v2 import create_message
            from pollypm.models import SessionConfig
            from pollypm.onboarding import default_control_args

            config = api.supervisor.config
            session_cfg = config.sessions.get(context.session_name)
            project_key = session_cfg.project if session_cfg else "pollypm"
            triage_name = f"triage_{project_key}"

            # Auto-create and launch the triage session if it doesn't exist
            if triage_name not in config.sessions:
                controller_name = config.pollypm.controller_account
                controller = config.accounts.get(controller_name)
                if controller is None:
                    return False
                project = config.projects.get(project_key)
                cwd = project.path if project else config.project.root_dir
                triage_session = SessionConfig(
                    name=triage_name,
                    role="triage",
                    provider=controller.provider,
                    account=controller_name,
                    cwd=cwd,
                    project=project_key,
                    window_name=f"triage-{project_key}",
                    prompt=triage_prompt(),
                    agent_profile="triage",
                    args=default_control_args(
                        controller.provider,
                        open_permissions=config.pollypm.open_permissions_by_default,
                        role="triage",
                    ),
                )
                config.sessions[triage_name] = triage_session
                write_config(config, api.supervisor.config_path, force=True)
                # Actually launch the tmux window
                api.supervisor.launch_session(triage_name)

            # Check if the tmux window exists — launch if not
            storage = api.supervisor.storage_closet_session_name()
            if api.supervisor.tmux.has_session(storage):
                windows = {w.name for w in api.supervisor.tmux.list_windows(storage)}
                if f"triage-{project_key}" not in windows:
                    api.supervisor.launch_session(triage_name)

            # Deliver briefing via inbox — tracked, audited, single-line delivery
            snippet = (context.pane_text or "").strip()[-200:] if context.pane_text else "no snapshot"
            create_message(
                config.project.root_dir,
                sender="heartbeat",
                subject=f"Triage: {context.session_name} — {reason[:40]}",
                to=triage_name,
                owner=triage_name,
                body=(
                    f"Session '{context.session_name}' needs attention. "
                    f"Reason: {reason}. "
                    f"Recent output: {snippet}. "
                    f"Check pm mail and pm status, then take action."
                ),
            )
            # The inbox_delivery job will deliver it on the next heartbeat cycle,
            # or we can trigger immediate delivery
            try:
                from pollypm.inbox_delivery import deliver_pending_messages
                deliver_pending_messages(config)
            except Exception:  # noqa: BLE001
                pass  # Heartbeat cycle will pick it up

            api.supervisor.store.record_event(
                context.session_name, "triage_activated",
                f"Activated triage session {triage_name}: {reason[:80]}",
            )
            return True
        except Exception:  # noqa: BLE001
            return False

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

    def _should_queue_followup(self, context: HeartbeatSessionContext) -> bool:
        cursor = context.cursor
        if cursor is None:
            return True
        if cursor.last_verdict != "needs_followup":
            return True
        if cursor.last_snapshot_hash == context.snapshot_hash:
            return False
        # The snapshot changed, but if the ONLY new content is our own
        # heartbeat messages, skip the followup to avoid a feedback loop.
        delta = (context.transcript_delta or "").strip()
        if delta:
            non_heartbeat_lines = [
                line for line in delta.splitlines()
                if line.strip() and not line.strip().startswith(("H:", "H: Heartbeat"))
            ]
            if not non_heartbeat_lines:
                return False
        return True

    # Rate limit nudges: max once per session per 10 minutes
    # Rate limits for worker nudges
    _NUDGE_COOLDOWN_SECONDS = 600
    _MAX_NUDGES_BEFORE_RECOVERY = 6

    def _nudge_stalled_worker(self, api, context: HeartbeatSessionContext) -> None:
        """Send a targeted nudge to a stalled WORKER. Never targets the operator."""
        if context.role != "worker":
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

    def _detect_worker_completion(self, api, context: HeartbeatSessionContext) -> None:
        """Detect completed work and ensure the user knows about it.

        Checks for:
        1. Open inbox threads the worker hasn't replied to → re-poke
        2. Worker idle with recent commits/changes and no user interaction
           in the last 5 minutes → notify Polly about the completed work
        """
        try:
            from datetime import UTC, datetime
            from pollypm.inbox_v2 import create_message, list_messages, mark_delivered

            config = api.supervisor.config
            root = config.project.root_dir
            store = api.supervisor.store

            # Re-poke stale delivered threads
            worker_threads = [
                m for m in list_messages(root, status="open")
                if m.to == context.session_name
            ]
            stale = [
                m for m in worker_threads
                if m.delivery_state == "delivered" and m.message_count <= 1
            ]
            for m in stale:
                mark_delivered(root, m.id, state="pending")

            # Check for completed work with no user awareness:
            # Worker is idle (same snapshot 2x) + has recent commits + no recent
            # user interaction (no send_input from human in last 5 min)
            hashes = api.recent_snapshot_hashes(context.session_name, limit=2)
            is_idle = len(hashes) >= 2 and hashes[0] == hashes[1]
            if not is_idle:
                return

            # Cooldown
            last = store.last_event_at(context.session_name, "completion_detected")
            if last:
                age = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
                if age < 600:
                    return

            # Check for recent commits (actual work product)
            session = config.sessions.get(context.session_name)
            if not session:
                return
            project = config.projects.get(session.project)
            if not project:
                return

            import subprocess
            try:
                result = subprocess.run(
                    ["git", "log", "--oneline", "--since=2 hours ago", "-5"],
                    cwd=project.path, capture_output=True, text=True, timeout=5,
                )
                recent_commits = result.stdout.strip() if result.returncode == 0 else ""
            except Exception:  # noqa: BLE001
                recent_commits = ""

            if not recent_commits:
                return  # No work product — don't notify

            # Check if user interacted recently (within 5 min)
            last_human = store.last_event_at(context.session_name, "send_input")
            if last_human:
                human_age = (datetime.now(UTC) - datetime.fromisoformat(last_human)).total_seconds()
                if human_age < 300:
                    return  # User is actively in this session

            # Work was done, worker is idle, user hasn't interacted → tell Polly
            create_message(
                root,
                sender="heartbeat",
                subject=f"Work completed: {session.project} — review needed",
                to="polly",
                owner="polly",
                body=(
                    f"Worker `{context.session_name}` on **{session.project}** is idle "
                    f"with recent commits:\n\n```\n{recent_commits}\n```\n\n"
                    f"Review the work, verify it meets the user's goal, and notify the user "
                    f"with `pm notify`."
                ),
            )
            store.record_event(
                context.session_name, "completion_detected",
                f"Worker idle with commits — notified Polly",
            )
        except Exception:  # noqa: BLE001
            pass

    def _has_pending_work(self, api, context: HeartbeatSessionContext) -> bool:
        """Check if a worker's project has ready or in-progress issues, or open inbox items."""
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
            # Check inbox for messages addressed to this worker
            from pollypm.inbox_v2 import list_messages
            inbox_items = [
                m for m in list_messages(config.project.root_dir, status="open")
                if m.to == context.session_name
            ]
            if inbox_items:
                return True
            return False
        except Exception:  # noqa: BLE001
            return False  # Assume no work on error — don't false-alert

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
