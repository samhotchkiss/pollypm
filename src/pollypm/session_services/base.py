"""Session service protocol and data types.

The SessionService is dumb infrastructure — it does what it's told.
It owns session lifecycle mechanics (create, destroy, capture, send)
but makes no policy decisions (when to recover, failover, or escalate).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(slots=True)
class SessionHandle:
    """Snapshot of a managed session's state."""

    name: str
    provider: str
    account: str
    window_name: str
    pane_id: str | None
    tmux_session: str
    cwd: str
    log_path: Path | None = None


@dataclass(slots=True)
class SessionHealth:
    """Raw health signals for a session — no classification, just facts.

    The heartbeat layer owns classification (active, idle, stuck, etc.).
    The session service just reports what it sees.
    """

    window_present: bool
    pane_alive: bool
    pane_dead: bool
    pane_command: str | None
    pane_text: str


@dataclass(slots=True)
class TranscriptStream:
    """Pointer to a session's transcript data."""

    path: Path
    offset: int = 0
    delta: str = ""


class SessionService(Protocol):
    """Protocol for session lifecycle management.

    Implementations own all terminal/pane mechanics. The default
    implementation uses tmux; alternatives could use other terminal
    multiplexers or headless containers.
    """

    name: str

    def create(
        self,
        name: str,
        provider: str,
        account: str,
        cwd: Path,
        prompt: str | None = None,
        *,
        command: str | None = None,
        window_name: str | None = None,
        log_path: Path | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> SessionHandle:
        """Create and stabilize a new session.

        The implementation handles provider-specific startup automation
        (dismissing prompts, detecting ready state).
        """
        ...

    def destroy(self, name: str) -> None:
        """Tear down a session — kill the pane/window, clean up resources."""
        ...

    def get(self, name: str) -> SessionHandle | None:
        """Look up a session by name. Returns None if not found."""
        ...

    def list(self) -> list[SessionHandle]:
        """List all managed sessions with their current state."""
        ...

    def health(self, name: str, *, capture_lines: int = 200) -> SessionHealth:
        """Collect raw health signals for a session.

        Returns pane state and text — the caller classifies (active,
        idle, stuck, etc.) based on these signals.
        """
        ...

    def is_turn_active(self, name: str) -> bool:
        """Check whether the agent is currently in an active turn."""
        ...

    def capture(self, name: str, lines: int = 200) -> str:
        """Capture the visible pane text for a session."""
        ...

    def send(self, name: str, text: str, *, press_enter: bool = True) -> None:
        """Send text into a session's pane.

        This is raw mechanics — no lease checking, no owner prefixing.
        The supervisor wraps this with policy.
        """
        ...

    def transcript(self, name: str) -> TranscriptStream | None:
        """Access the transcript stream for a session, if available."""
        ...

    def switch_account(
        self,
        name: str,
        new_account: str,
        new_provider: str,
        *,
        command: str | None = None,
        cwd: Path | None = None,
        prompt: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> SessionHandle:
        """Switch a session to a different account/provider.

        Tears down the current pane and relaunches with new credentials.
        The supervisor decides WHEN to switch; this method executes it.
        """
        ...
