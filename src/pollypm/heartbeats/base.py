from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class HeartbeatCursor:
    session_name: str
    source_path: str
    last_offset: int
    last_processed_at: str | None = None
    last_snapshot_hash: str = ""
    last_verdict: str = ""
    last_reason: str = ""
    #: Consecutive ticks that produced no fresh transcript output for a
    #: session that *could* legitimately be working (worker with pending
    #: work, etc.). Reset to 0 on any tick where the classifier produced
    #: a non-"no new transcript" reason. After ``QUIET_TICKS_FOR_FLOW_MARKER``
    #: ticks, the heartbeat marks the session ``flow=quiet`` so ``pm
    #: status`` distinguishes "alive but silent" from "actively working."
    #: Generalises the per-question detection added in #1493 — see #1501
    #: heartbeat-progress-signal.
    quiet_tick_count: int = 0


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
    pane_stopped: bool = False


@dataclass(slots=True)
class HeartbeatUnmanagedWindow:
    tmux_session: str
    window_name: str
    pane_id: str
    pane_command: str
    pane_dead: bool
    pane_path: str


class HeartbeatBackend(Protocol):
    """Mechanical session-health sweep (runs inside a roster-scheduled job).

    The ``api`` argument is duck-typed — today it's a
    :class:`pollypm.heartbeats.api.SupervisorHeartbeatAPI`. A stable Protocol
    for the API surface was removed with the legacy heartbeat dispatch
    (issue #166) because only one caller exists; re-add a Protocol here if
    a second backend implementation lands.
    """

    name: str

    def run(self, api: Any, *, snapshot_lines: int = 200) -> list[Any]: ...
