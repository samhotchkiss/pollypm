"""LaunchPlanner protocol and data types.

The LaunchPlanner turns a declared ``PollyPMConfig`` into a list of
concrete ``SessionLaunchSpec`` entries — applying runtime account
overrides, sanitizing provider arguments, wrapping launch commands in
their runtime, and deciding which tmux session should host each window.

It is policy code: Supervisor delegates to it rather than owning the
logic itself so alternative planners (remote dispatch, cluster-aware
placement, etc.) can ship as plugins.

The default implementation (:class:`pollypm.plugins_builtin.default_launch_planner`)
preserves historical Supervisor behavior byte-for-byte.
"""

from __future__ import annotations

from typing import Protocol

from pollypm.models import SessionConfig, SessionLaunchSpec


class LaunchPlanner(Protocol):
    """Protocol for session launch planning.

    Implementations produce the launch plan the Supervisor uses to
    create tmux windows. The protocol is intentionally small — just
    the four public methods Supervisor needs to delegate.
    """

    name: str

    def plan_launches(
        self, *, controller_account: str | None = None
    ) -> list[SessionLaunchSpec]:
        """Return the launch plan for every enabled session.

        Inputs: an optional ``controller_account`` that overrides
        control-role sessions. Output: a list of ``SessionLaunchSpec``
        entries — one per enabled session whose account resolves
        successfully.
        """
        ...

    def effective_session(
        self,
        session: SessionConfig,
        controller_account: str | None = None,
    ) -> SessionConfig:
        """Return ``session`` with runtime account overrides applied."""
        ...

    def tmux_session_for_launch(self, launch: SessionLaunchSpec) -> str:
        """Return the tmux session name that should host ``launch``."""
        ...

    def launch_by_session(self, session_name: str) -> SessionLaunchSpec:
        """Return the ``SessionLaunchSpec`` for ``session_name``.

        Raises ``KeyError`` when no such session exists.
        """
        ...

    def invalidate_cache(self) -> None:
        """Drop any cached launch plan so the next call recomputes."""
        ...
