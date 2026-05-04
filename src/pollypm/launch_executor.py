"""Executor that turns a :class:`LaunchPlan` into supervisor calls.

The audit (#896) cited the gap: ``cli.up()`` consults
:func:`plan_launch` but then maintains a separate hand-written
launch flow that does not interpret ``plan.actions``. The state
machine could drift from production behavior with no failing test.
This module is the structural fix.

Architecture:

* :class:`LaunchPlanExecutor` accepts a supervisor, a probe, and
  the config path on construction.
* :meth:`LaunchPlanExecutor.execute` walks ``plan.actions`` in
  order and dispatches each one to a typed method.
* Every action handler is idempotent — running ``ENSURE_MAIN_
  SESSION`` when the session already exists is a no-op. This
  matches the contract that :func:`plan_launch` is *declarative*
  (the action list says what should be true) rather than
  imperative (a sequence of mutations).
* The executor returns the final ``exit_code`` from
  attach/switch_client (typer's ``Exit(code=...)`` value), or
  ``None`` when the plan ended in ``FOCUS_CONSOLE``.

Tests in :mod:`tests.test_launch_executor` exercise each action
in isolation against a synthetic supervisor — no live tmux
required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from pollypm.launch_state import LaunchAction, LaunchPlan, LaunchProbe, LaunchState


logger = logging.getLogger(__name__)


_COCKPIT_WINDOW_NAME: str = "PollyPM"


# ---------------------------------------------------------------------------
# Supervisor surface (Protocol)
# ---------------------------------------------------------------------------


class _SupervisorLike(Protocol):
    """Minimum supervisor surface the executor needs."""

    config: object
    tmux: object

    def bootstrap_tmux(self, **kwargs) -> str: ...
    def ensure_console_window(self) -> None: ...
    def focus_console(self) -> None: ...
    def ensure_heartbeat_schedule(self) -> None: ...
    def storage_closet_session_name(self) -> str: ...

    # Optional — present on real supervisors but not on every fake.
    # The executor checks ``hasattr`` before calling.
    # def console_command(self) -> str: ...


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutionResult:
    """Outcome of executing a :class:`LaunchPlan`.

    ``actions_run`` is the ordered tuple of actions the executor
    actually dispatched (may be a prefix of ``plan.actions`` if a
    handler short-circuited). ``exit_code`` is what typer should
    raise via ``Exit(code=...)``; ``None`` means no ``Exit`` is
    needed. ``messages`` accumulates user-facing status lines
    (``cli.up()`` echoes them through typer)."""

    actions_run: tuple[LaunchAction, ...]
    exit_code: int | None
    messages: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


#: Actions that must succeed before later mutating actions in the
#: same plan are allowed to run. ``#908`` audit: a failed bootstrap
#: was still followed by ``ENSURE_MAIN_SESSION``, ``SCHEDULE_HEARTBEAT``,
#: ``START_RAIL_DAEMON``, and the attach action — leaving the cockpit
#: half-built. ``BOOTSTRAP_LAUNCHES`` and ``ENSURE_STORAGE_CLOSET``
#: are the only critical actions today; the rest are either
#: idempotent reconciliation no-ops (``ENSURE_CONSOLE_WINDOW``,
#: ``ENSURE_MAIN_SESSION``), best-effort daemons
#: (``START_RAIL_DAEMON``), informational respawns
#: (``RESPAWN_SHELL``, ``RESPAWN_RAIL``), or terminal user-facing
#: actions whose own non-zero exit code already short-circuits via
#: the first-non-None capture below.
_CRITICAL_LAUNCH_ACTIONS: frozenset[LaunchAction] = frozenset(
    {
        LaunchAction.BOOTSTRAP_LAUNCHES,
        LaunchAction.ENSURE_STORAGE_CLOSET,
    }
)


class LaunchPlanExecutor:
    """Translate a :class:`LaunchPlan` into supervisor calls.

    Handlers are idempotent. ``execute`` walks the plan's action
    tuple in order; each dispatched handler may return an
    ``int`` exit code (terminal actions like ``ATTACH_SESSION``)
    or ``None`` (non-terminal actions). The first non-None exit
    code becomes the executor's final result.

    Failure short-circuit (#908): when a *critical* action — see
    :data:`_CRITICAL_LAUNCH_ACTIONS` — returns a non-None exit code,
    the executor halts immediately. Subsequent ``ensure_main_session``,
    ``schedule_heartbeat``, ``start_rail_daemon``, and attach/switch/
    focus actions are skipped so a failed first launch cannot leave
    a partial cockpit running. Non-critical action failures still
    record their exit code but do not stop the run.
    """

    def __init__(
        self,
        supervisor: _SupervisorLike,
        *,
        probe: LaunchProbe,
        config_path: Path,
        status_emit: Callable[[str], None] | None = None,
        rail_daemon_spawner: Callable[[Path], None] | None = None,
        before_attach: Callable[[], None] | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.probe = probe
        self.config_path = config_path
        self._status_emit = status_emit or (lambda _msg: None)
        self._rail_daemon_spawner = rail_daemon_spawner
        self._before_attach = before_attach
        self._before_attach_ran = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute(self, plan: LaunchPlan) -> ExecutionResult:
        """Execute every action in ``plan.actions``."""
        if plan.state is LaunchState.UNSUPPORTED:
            # Defensive — UNSUPPORTED is supposed to be rejected
            # by the caller before the executor sees it. If it
            # leaks through, return an exit_code instead of
            # mutating tmux.
            return ExecutionResult(
                actions_run=(LaunchAction.FAIL_CLOSED,),
                exit_code=2,
                messages=(plan.reason,),
            )

        run: list[LaunchAction] = []
        messages: list[str] = []
        exit_code: int | None = None

        for action in plan.actions:
            handler = self._dispatch.get(action)
            if handler is None:
                logger.warning(
                    "launch executor: no handler for %s — skipping",
                    action.value,
                )
                continue
            try:
                action_exit, action_messages = handler(self)
            except Exception:  # noqa: BLE001 — log and continue
                logger.exception(
                    "launch executor: handler for %s raised", action.value,
                )
                continue
            run.append(action)
            messages.extend(action_messages)
            if action_exit is not None and exit_code is None:
                exit_code = action_exit
            # #908: a failure in a critical action (bootstrap /
            # storage closet) must halt the plan. Continuing would
            # let create_session, heartbeat, the rail daemon, and
            # attach/switch/focus run on top of a half-bootstrapped
            # cockpit — exactly the partial-state failure the
            # fail-closed launch contract forbids.
            if (
                action_exit is not None
                and action_exit != 0
                and action in _CRITICAL_LAUNCH_ACTIONS
            ):
                logger.warning(
                    "launch executor: critical action %s failed "
                    "(exit=%s) — halting plan",
                    action.value,
                    action_exit,
                )
                messages.append(
                    f"halting launch after {action.value} failure "
                    f"(exit={action_exit})"
                )
                break

        return ExecutionResult(
            actions_run=tuple(run),
            exit_code=exit_code,
            messages=tuple(messages),
        )

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------
    #
    # Each handler returns ``(exit_code | None, messages: list[str])``.
    # Handlers swallow any expected error and emit a status message;
    # unexpected errors propagate to ``execute`` which logs and
    # continues.

    def _on_ensure_storage_closet(self) -> tuple[int | None, list[str]]:
        # Storage closet is created as a side effect of
        # bootstrap_tmux. Calling ENSURE_STORAGE_CLOSET directly
        # is a no-op; the canonical path is BOOTSTRAP_LAUNCHES,
        # which is always paired with this action when needed.
        return (None, [])

    def _on_bootstrap_launches(self) -> tuple[int | None, list[str]]:
        try:
            controller_account = self.supervisor.bootstrap_tmux(
                skip_probe=True,
                on_status=self._status_emit,
            )
        except RuntimeError as exc:
            return (1, [f"bootstrap failed: {exc}"])
        # Idempotent reconciliation may return any account name
        # (or "" when no account info is available — fakes).
        messages: list[str] = []

        # #912: restore the user-facing launch status line that
        # cli.up() emitted before the #896 executor refactor:
        # ``Created tmux session <name> with controller
        # <email_or_account> [<provider>]``. The line is the only
        # confirmation users have that the bootstrap actually
        # selected a controller (and which provider/identity it
        # picked) — important when failover is enabled and a
        # secondary account took over. Lookups are best-effort: a
        # synthetic supervisor with no real ``config`` (the unit
        # test fakes) just falls through to the debug line below.
        config = getattr(self.supervisor, "config", None)
        accounts = getattr(config, "accounts", None) if config is not None else None
        project = getattr(config, "project", None) if config is not None else None
        session_name = getattr(project, "tmux_session", None) if project is not None else None
        account = None
        if accounts is not None and controller_account:
            try:
                account = accounts[controller_account]
            except (KeyError, TypeError):
                account = None
        if session_name and account is not None:
            email = getattr(account, "email", None) or controller_account
            provider = getattr(account, "provider", None)
            provider_value = getattr(provider, "value", provider)
            messages.append(
                f"Created tmux session {session_name} with controller "
                f"{email} [{provider_value}]"
            )

        # Keep the post-#896 debug line as well so launch transcripts
        # still record the raw account key for grep/log consumers.
        messages.append(f"bootstrap controller={controller_account!r}")
        return (None, messages)

    def _on_ensure_main_session(self) -> tuple[int | None, list[str]]:
        # Only meaningful in the RESTORE_FROM_CLOSET path: closet
        # is alive but the main session vanished. Re-create the
        # console window with the standard window options.
        if self.probe.main_session_alive:
            return (None, [])
        # #1098 — RESTORE_FROM_CLOSET now also runs BOOTSTRAP_LAUNCHES
        # earlier in the plan, which calls ``_reconcile_existing`` —
        # that path already creates the main session as a side effect.
        # The probe is captured before the plan executes, so its
        # ``main_session_alive`` is still False even when the live
        # tmux session is now present. Check live state to avoid a
        # redundant ``create_session`` call that would raise
        # "session already exists" and get swallowed silently.
        tmux = self.supervisor.tmux
        has_session = getattr(tmux, "has_session", None)
        if callable(has_session):
            try:
                if has_session(self.probe.main_session_name):
                    return (None, [])
            except Exception:  # noqa: BLE001
                pass
        try:
            console_command = getattr(self.supervisor, "console_command", None)
            command = console_command() if callable(console_command) else "$SHELL"
            tmux.create_session(
                self.probe.main_session_name,
                _COCKPIT_WINDOW_NAME,
                command,
                remain_on_exit=False,
            )
            for option in ("allow-passthrough", "window-size", "aggressive-resize"):
                value = "on" if option != "window-size" else "latest"
                tmux.set_window_option(
                    f"{self.probe.main_session_name}:{_COCKPIT_WINDOW_NAME}",
                    option,
                    value,
                )
        except Exception:  # noqa: BLE001
            return (None, [])
        return (None, [
            f"restored main session {self.probe.main_session_name}"
        ])

    def _on_ensure_console_window(self) -> tuple[int | None, list[str]]:
        ensure = getattr(self.supervisor, "ensure_console_window", None)
        if callable(ensure):
            ensure()
        return (None, [])

    def _on_respawn_shell(self) -> tuple[int | None, list[str]]:
        # The probe carries enough state to identify the dead
        # console pane; the legacy ensure_console_window path
        # already covers a respawn so delegate to it. The action
        # is recorded for observability.
        return self._on_ensure_console_window()

    def _on_respawn_rail(self) -> tuple[int | None, list[str]]:
        # The cockpit rail layout owner (CockpitRouter.ensure_
        # cockpit_layout + Supervisor.start_cockpit_tui) is wired through
        # the before-attach hook. Run it here as well as at attach time so
        # RECOVER_DEAD_RAIL is not just an observable no-op.
        self._run_before_attach_once()
        return (None, [])

    def _on_schedule_heartbeat(self) -> tuple[int | None, list[str]]:
        ensure = getattr(self.supervisor, "ensure_heartbeat_schedule", None)
        if callable(ensure):
            ensure()
        return (None, [])

    def _on_start_rail_daemon(self) -> tuple[int | None, list[str]]:
        if self._rail_daemon_spawner is not None:
            try:
                self._rail_daemon_spawner(self.config_path)
            except Exception:  # noqa: BLE001 — best-effort
                pass
        return (None, [])

    def _on_attach_session(self) -> tuple[int | None, list[str]]:
        messages = self._run_before_attach_once()
        tmux = self.supervisor.tmux
        attach = getattr(tmux, "attach_session", None)
        if not callable(attach):
            return (None, messages)
        return (
            int(attach(self.probe.main_session_name) or 0),
            messages,
        )

    def _on_switch_client(self) -> tuple[int | None, list[str]]:
        messages = self._run_before_attach_once()
        focus = getattr(self.supervisor, "focus_console", None)
        if callable(focus):
            focus()
        tmux = self.supervisor.tmux
        switch = getattr(tmux, "switch_client", None)
        if not callable(switch):
            return (None, messages)
        return (
            int(switch(self.probe.main_session_name) or 0),
            [*messages, f"Switching to tmux session {self.probe.main_session_name}"],
        )

    def _on_focus_console(self) -> tuple[int | None, list[str]]:
        messages = self._run_before_attach_once()
        focus = getattr(self.supervisor, "focus_console", None)
        if callable(focus):
            focus()
        return (
            None,
            [*messages, f"Already inside tmux session {self.probe.main_session_name}"],
        )

    def _on_fail_closed(self) -> tuple[int | None, list[str]]:
        return (2, ["fail_closed"])

    def _run_before_attach_once(self) -> list[str]:
        hook = self._before_attach
        if hook is None or self._before_attach_ran:
            return []
        self._before_attach_ran = True
        try:
            hook()
        except Exception as exc:  # noqa: BLE001
            logger.exception("launch executor: before_attach hook failed")
            return [f"cockpit layout failed before attach: {exc}"]
        return []

    # ------------------------------------------------------------------
    # Dispatch table
    # ------------------------------------------------------------------
    @property
    def _dispatch(self) -> dict[
        LaunchAction,
        Callable[
            ["LaunchPlanExecutor"], tuple[int | None, list[str]]
        ],
    ]:
        return {
            LaunchAction.ENSURE_STORAGE_CLOSET: type(self)._on_ensure_storage_closet,
            LaunchAction.BOOTSTRAP_LAUNCHES: type(self)._on_bootstrap_launches,
            LaunchAction.ENSURE_MAIN_SESSION: type(self)._on_ensure_main_session,
            LaunchAction.ENSURE_CONSOLE_WINDOW: type(self)._on_ensure_console_window,
            LaunchAction.RESPAWN_SHELL: type(self)._on_respawn_shell,
            LaunchAction.RESPAWN_RAIL: type(self)._on_respawn_rail,
            LaunchAction.SCHEDULE_HEARTBEAT: type(self)._on_schedule_heartbeat,
            LaunchAction.START_RAIL_DAEMON: type(self)._on_start_rail_daemon,
            LaunchAction.ATTACH_SESSION: type(self)._on_attach_session,
            LaunchAction.SWITCH_CLIENT: type(self)._on_switch_client,
            LaunchAction.FOCUS_CONSOLE: type(self)._on_focus_console,
            LaunchAction.FAIL_CLOSED: type(self)._on_fail_closed,
        }
