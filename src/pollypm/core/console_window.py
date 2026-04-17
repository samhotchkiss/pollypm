"""Cockpit console-window orchestration.

The "console" is the top-level tmux window that hosts the PollyPM cockpit
rail + a mounted worker pane. It has a small but fiddly lifecycle:

* create if missing (with ``aggressive-resize`` / ``window-size latest`` so
  the rail doesn't SIGWINCH on attach)
* detect + repair when the rail pane dies and leaves only a worker behind
* focus / select on demand

Historically this lived inline on :class:`pollypm.supervisor.Supervisor`.
Step 9 of the Supervisor decomposition (#186) lifts it out so the
orchestration surface is one clearly-named class and Supervisor becomes
a thin caller.

The manager is deliberately stateless: it holds references to the config
and the tmux client, plus two helper callables the Supervisor still owns
(``storage_closet_session_name`` and ``plan_launches`` — both needed only
during the rare repair path). Callers construct a manager per-supervisor
and throw it away at process shutdown.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pollypm.config import PollyPMConfig
    from pollypm.models import SessionLaunchSpec
    from pollypm.session_services.tmux import TmuxSessionService


#: Name of the PollyPM console/cockpit window inside a tmux session.
CONSOLE_WINDOW = "PollyPM"


class ConsoleWindowManager:
    """Own the ``PollyPM`` console window's create / repair / focus lifecycle.

    The manager is always bound to a concrete :class:`TmuxSessionService`
    so it can talk to tmux through the same client Supervisor uses. It
    reads configuration from the supplied :class:`PollyPMConfig` and
    dispatches two callbacks back into the Supervisor for information it
    doesn't own (storage-closet naming + launch plan access).
    """

    __slots__ = (
        "_config",
        "_session_service",
        "_storage_closet_session_name",
        "_plan_launches",
    )

    def __init__(
        self,
        *,
        config: "PollyPMConfig",
        session_service: "TmuxSessionService",
        storage_closet_session_name: Callable[[], str],
        plan_launches: Callable[[], list["SessionLaunchSpec"]],
    ) -> None:
        self._config = config
        self._session_service = session_service
        self._storage_closet_session_name = storage_closet_session_name
        self._plan_launches = plan_launches

    # ── Inspection ─────────────────────────────────────────────────────────

    @property
    def window_name(self) -> str:
        """Return the tmux window name ``"PollyPM"``."""
        return CONSOLE_WINDOW

    def console_command(self) -> str:
        """Shell command for the cockpit rail pane (runs before TUI launches).

        ``"bash -l"`` gives us a hot shell; the cockpit TUI is sent to the
        pane separately so a SIGWINCH during layout doesn't kill it.
        """
        return "bash -l"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def ensure(self) -> None:
        """Create the console window if the PollyPM tmux session is running.

        Idempotent: no-op if the session is absent or the window already
        exists (in which case we run the repair path). Sets the tmux
        window options that keep the rail visually stable under resize.
        """
        tmux = self._session_service.tmux
        tmux_session = self._config.project.tmux_session
        if not tmux.has_session(tmux_session):
            return
        if self._window_exists(tmux_session):
            self.repair_if_broken(tmux_session)
            return
        tmux.create_window(
            tmux_session, CONSOLE_WINDOW, self.console_command(), detached=True,
        )
        target = f"{tmux_session}:{CONSOLE_WINDOW}"
        tmux.set_window_option(target, "allow-passthrough", "on")
        tmux.set_window_option(target, "window-size", "latest")
        tmux.set_window_option(target, "aggressive-resize", "on")
        tmux.set_pane_history_limit(target, 200)

    def focus(self) -> None:
        """Ensure the console exists and select its window."""
        tmux = self._session_service.tmux
        tmux_session = self._config.project.tmux_session
        if not tmux.has_session(tmux_session):
            return
        self.ensure()
        tmux.select_window(f"{tmux_session}:{CONSOLE_WINDOW}")

    def repair_if_broken(self, tmux_session: str) -> None:
        """Detect + repair a cockpit window whose rail pane died.

        Failure mode: the cockpit had two panes (rail on the left, a
        worker mounted on the right). If the rail pane exits the worker
        survives, leaving a one-pane cockpit that doesn't look right.
        This method re-parks the worker back in its storage-closet home,
        clears the cockpit-state pointer, and respawns the rail pane.

        Safe to call whenever: no-op when the cockpit has the expected
        pane count or when the surviving pane isn't the tracked worker.
        """
        tmux = self._session_service.tmux
        target = f"{tmux_session}:{CONSOLE_WINDOW}"
        try:
            panes = tmux.list_panes(target)
        except Exception:  # noqa: BLE001
            return
        if len(panes) != 1:
            return
        pane = panes[0]

        state_path = self._config.project.base_dir / "cockpit_state.json"
        try:
            state_data = json.loads(state_path.read_text()) if state_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            state_data = {}
        saved_right_id = state_data.get("right_pane_id") if isinstance(state_data, dict) else None
        if not isinstance(saved_right_id, str) or saved_right_id != pane.pane_id:
            return

        # Park the surviving worker pane back to the storage closet before
        # respawning the rail.
        mounted_session = state_data.get("mounted_session") if isinstance(state_data, dict) else None
        if isinstance(mounted_session, str) and mounted_session:
            launch = next(
                (
                    item
                    for item in self._plan_launches()
                    if item.session.name == mounted_session
                ),
                None,
            )
            storage_session = self._storage_closet_session_name()
            if launch is not None and tmux.has_session(storage_session):
                try:
                    tmux.break_pane(pane.pane_id, storage_session, launch.window_name)
                except Exception:  # noqa: BLE001
                    pass

        if isinstance(state_data, dict):
            state_data.pop("right_pane_id", None)
            state_data.pop("mounted_session", None)
            try:
                state_path.write_text(json.dumps(state_data, indent=2) + "\n")
            except OSError:
                pass

        # Recreate the cockpit; if break_pane removed the last pane the
        # whole session is gone and we need create_session instead of
        # create_window.
        if not tmux.has_session(tmux_session):
            tmux.create_session(
                tmux_session, CONSOLE_WINDOW, self.console_command(), remain_on_exit=False,
            )
        else:
            try:
                remaining_panes = tmux.list_panes(target)
            except Exception:  # noqa: BLE001
                remaining_panes = []
            if len(remaining_panes) == 0:
                tmux.create_window(
                    tmux_session, CONSOLE_WINDOW, self.console_command(), detached=True,
                )
            else:
                tmux.respawn_pane(remaining_panes[0].pane_id, self.console_command())

        full_target = f"{tmux_session}:{CONSOLE_WINDOW}"
        tmux.set_window_option(full_target, "allow-passthrough", "on")
        tmux.set_window_option(full_target, "window-size", "latest")
        tmux.set_window_option(full_target, "aggressive-resize", "on")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _window_exists(self, tmux_session: str) -> bool:
        """Return True when the console window is present in ``tmux_session``."""
        tmux = self._session_service.tmux
        for window in tmux.list_windows(tmux_session):
            if window.name == CONSOLE_WINDOW:
                return True
        return False


__all__ = ["CONSOLE_WINDOW", "ConsoleWindowManager"]
