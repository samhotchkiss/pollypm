from __future__ import annotations

import re

from pollypm.heartbeats.base import HeartbeatBackend, HeartbeatSessionContext


class LocalHeartbeatBackend(HeartbeatBackend):
    name = "local"

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
        for context in api.list_sessions():
            self._process_session(api, context)
        api.record_event(
            "heartbeat",
            "heartbeat",
            f"Heartbeat sweep completed with {len(api.open_alerts())} open alerts",
        )
        return api.open_alerts()

    def _process_session(self, api, context: HeartbeatSessionContext) -> None:
        alerts: list[str] = []
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

        if context.previous_log_bytes is not None and context.source_bytes <= context.previous_log_bytes:
            api.raise_alert(
                context.session_name,
                "idle_output",
                "warn",
                f"No new pane output since the previous heartbeat for window {context.window_name}",
            )
            alerts.append("idle_output")
        else:
            api.clear_alert(context.session_name, "idle_output")

        if context.previous_snapshot_hash and context.previous_snapshot_hash == context.snapshot_hash:
            hashes = api.recent_snapshot_hashes(context.session_name, limit=3)
            if len(hashes) == 3 and len(set(hashes)) == 1:
                api.raise_alert(
                    context.session_name,
                    "suspected_loop",
                    "warn",
                    f"Window {context.window_name} has produced effectively the same snapshot for 3 heartbeats",
                )
                alerts.append("suspected_loop")
            else:
                api.clear_alert(context.session_name, "suspected_loop")
        else:
            api.clear_alert(context.session_name, "suspected_loop")

        combined_text = "\n".join(part for part in [context.transcript_delta, context.pane_text] if part).lower()
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
        else:
            api.clear_alert(context.session_name, "auth_broken")

        verdict, reason = self._classify(context)
        if verdict == "needs_followup":
            api.raise_alert(context.session_name, "needs_followup", "warn", reason)
            api.set_session_status(context.session_name, "needs_followup", reason=reason)
            alerts.append("needs_followup")
        else:
            api.clear_alert(context.session_name, "needs_followup")
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

    def _classify(self, context: HeartbeatSessionContext) -> tuple[str, str]:
        text = (context.transcript_delta or context.pane_text or "").strip()
        if not text:
            return "unclear", "No new transcript or pane output to classify"
        lowered = text.lower()
        if any(pattern in lowered for pattern in self._WAITING_PATTERNS):
            return "blocked", "Last turn appears to be waiting on operator input"
        if any(pattern in lowered for pattern in self._FOLLOWUP_PATTERNS):
            return "needs_followup", "Last turn suggests additional work remains"
        if any(pattern in lowered for pattern in self._DONE_PATTERNS):
            return "done", "Last turn appears complete"
        if re.search(r"\b(next|remaining|follow-up|follow up|still need)\b", lowered):
            return "needs_followup", "Last turn suggests additional work remains"
        if lowered.endswith("?"):
            return "blocked", "Last turn ended with a question for the operator"
        return "unclear", "Could not confidently classify the last turn"
