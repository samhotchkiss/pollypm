from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pollypm.heartbeats.types import Alert


@dataclass(slots=True)
class HeartbeatCursor:
    session_name: str
    source_path: str
    last_offset: int
    last_processed_at: str | None = None
    last_snapshot_hash: str = ""
    last_verdict: str = ""
    last_reason: str = ""


@dataclass(slots=True)
class HeartbeatSessionContext:
    session_name: str
    role: str
    project_key: str
    provider: str
    account_name: str
    cwd: str
    tmux_session: str
    window_name: str
    source_path: str
    source_bytes: int
    transcript_delta: str
    pane_text: str
    snapshot_path: str | None
    snapshot_hash: str
    pane_id: str | None
    pane_command: str | None
    pane_dead: bool
    window_present: bool
    previous_log_bytes: int | None
    previous_snapshot_hash: str | None
    cursor: HeartbeatCursor | None = None


@dataclass(slots=True)
class HeartbeatUnmanagedWindow:
    tmux_session: str
    window_name: str
    pane_id: str
    pane_command: str
    pane_dead: bool
    pane_path: str


class HeartbeatAPI(Protocol):
    def list_sessions(self) -> list[HeartbeatSessionContext]: ...

    def list_unmanaged_windows(self) -> list[HeartbeatUnmanagedWindow]: ...

    def get_cursor(self, session_name: str) -> HeartbeatCursor | None: ...

    def update_cursor(
        self,
        session_name: str,
        *,
        source_path: str,
        last_offset: int,
        snapshot_hash: str = "",
        verdict: str = "",
        reason: str = "",
    ) -> None: ...

    def record_observation(self, context: HeartbeatSessionContext) -> None: ...

    def record_checkpoint(self, context: HeartbeatSessionContext, *, alerts: list[str]) -> None: ...

    def record_event(self, session_name: str, event_type: str, message: str) -> None: ...

    def raise_alert(self, session_name: str, alert_type: str, severity: str, message: str) -> None: ...

    def clear_alert(self, session_name: str, alert_type: str) -> None: ...

    def open_alerts(self) -> list[Alert]: ...

    def set_session_status(self, session_name: str, status: str, *, reason: str = "") -> None: ...

    def mark_account_auth_broken(self, account_name: str, provider: str, *, reason: str) -> None: ...

    def recent_snapshot_hashes(self, session_name: str, *, limit: int = 3) -> list[str]: ...

    def recover_session(self, session_name: str, *, failure_type: str, message: str) -> None: ...

    def send_session_message(self, session_name: str, text: str, *, owner: str = "heartbeat") -> None: ...

    def queue_polly_followup(self, session_name: str, reason: str) -> None: ...


class HeartbeatBackend(Protocol):
    name: str

    def run(self, api: HeartbeatAPI, *, snapshot_lines: int = 200) -> list[Alert]: ...
