from __future__ import annotations

import re

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
            self._process_session(api, context)
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
                api.send_session_message(
                    "operator",
                    (
                        "Heartbeat follow-up: inspect unmanaged tmux window "
                        f"{window.window_name} in session {window.tmux_session}. "
                        "It is not part of the managed Polly launch plan."
                    ),
                    owner="heartbeat",
                )
        for alert_type in existing_alert_types - current_alert_types:
            api.clear_alert("heartbeat", alert_type)

    def _process_session(self, api, context: HeartbeatSessionContext) -> None:
        alerts: list[str] = []
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
                api.raise_alert(
                    context.session_name,
                    "suspected_loop",
                    "warn",
                    f"Window {context.window_name} has produced effectively the same snapshot for 3 heartbeats",
                )
                alerts.append("suspected_loop")
                # After 5 consecutive identical snapshots, notify operator AND nudge worker directly
                longer_hashes = api.recent_snapshot_hashes(context.session_name, limit=5)
                if len(longer_hashes) == 5 and len(set(longer_hashes)) == 1:
                    api.queue_polly_followup(
                        context.session_name,
                        f"Session idle for 5+ heartbeat cycles — may need a nudge or reassignment",
                    )
                    # Direct nudge to the worker — only for worker roles, not control sessions
                    if context.role == "worker":
                        api.send_session_message(
                            context.session_name,
                            "You appear stalled and additional work remains. "
                            "Stop looping, state the remaining task in one sentence, "
                            "execute the next concrete step now, and report verification or blocker.",
                            owner="heartbeat",
                        )
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
                if self._should_queue_followup(context):
                    api.queue_polly_followup(context.session_name, reason)
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
