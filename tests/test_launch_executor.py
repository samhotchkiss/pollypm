"""Tests for the launch-plan executor (#896)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pollypm.launch_executor import (
    ExecutionResult,
    LaunchPlanExecutor,
)
from pollypm.launch_state import (
    LaunchAction,
    LaunchContext,
    LaunchPlan,
    LaunchProbe,
    LaunchState,
    plan_launch,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeTmux:
    """Records every method call so tests can assert on the
    sequence of supervisor/tmux operations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.has_session_returns: dict[str, bool] = {}
        self.attach_returns: int = 0
        self.switch_returns: int = 0

    def has_session(self, name: str) -> bool:
        self.calls.append(("has_session", (name,), {}))
        return self.has_session_returns.get(name, False)

    def current_session_name(self) -> str | None:
        self.calls.append(("current_session_name", (), {}))
        return None

    def create_session(self, *args, **kwargs) -> None:
        self.calls.append(("create_session", args, kwargs))

    def set_window_option(self, *args, **kwargs) -> None:
        self.calls.append(("set_window_option", args, kwargs))

    def attach_session(self, name: str) -> int:
        self.calls.append(("attach_session", (name,), {}))
        return self.attach_returns

    def switch_client(self, name: str) -> int:
        self.calls.append(("switch_client", (name,), {}))
        return self.switch_returns


class _FakeSupervisor:
    """Records every supervisor-level call."""

    def __init__(self, *, bootstrap_raises: Exception | None = None) -> None:
        self.tmux = _FakeTmux()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._bootstrap_raises = bootstrap_raises

    def bootstrap_tmux(self, **kwargs) -> str:
        self.calls.append(("bootstrap_tmux", (), kwargs))
        if self._bootstrap_raises is not None:
            raise self._bootstrap_raises
        return "claude_main"

    def ensure_console_window(self) -> None:
        self.calls.append(("ensure_console_window", (), {}))

    def ensure_heartbeat_schedule(self) -> None:
        self.calls.append(("ensure_heartbeat_schedule", (), {}))

    def focus_console(self) -> None:
        self.calls.append(("focus_console", (), {}))

    def storage_closet_session_name(self) -> str:
        return "pollypm-storage-closet"

    def console_command(self) -> str:
        return "/bin/bash"


def _probe(**kwargs: Any) -> LaunchProbe:
    base = dict(
        main_session_name="pollypm",
        closet_session_name="pollypm-storage-closet",
        main_session_alive=False,
        closet_session_alive=False,
        console_pane_alive=True,
        rail_pane_alive=True,
        rail_pane_running_non_shell=True,
        current_tmux_session=None,
    )
    base.update(kwargs)
    return LaunchProbe(**base)


def _executor(
    *,
    probe: LaunchProbe,
    supervisor: _FakeSupervisor | None = None,
    rail_daemon_calls: list[Path] | None = None,
    before_attach=None,
) -> tuple[LaunchPlanExecutor, _FakeSupervisor]:
    sup = supervisor or _FakeSupervisor()
    spawner = None
    if rail_daemon_calls is not None:
        spawner = lambda p: rail_daemon_calls.append(p)
    exe = LaunchPlanExecutor(
        sup,
        probe=probe,
        config_path=Path("/tmp/pollypm.toml"),
        rail_daemon_spawner=spawner,
        before_attach=before_attach,
    )
    return exe, sup


# ---------------------------------------------------------------------------
# FIRST_LAUNCH — bootstrap path
# ---------------------------------------------------------------------------


def test_first_launch_invokes_bootstrap_and_attach() -> None:
    """The audit's headline ask: the executor must actually run
    plan.actions. FIRST_LAUNCH must call bootstrap_tmux and then
    attach the user."""
    probe = _probe()  # nothing alive
    plan = plan_launch(probe)
    assert plan.state is LaunchState.FIRST_LAUNCH

    rail_daemon_calls: list[Path] = []
    exe, sup = _executor(probe=probe, rail_daemon_calls=rail_daemon_calls)
    result = exe.execute(plan)

    method_names = [c[0] for c in sup.calls]
    assert "bootstrap_tmux" in method_names
    assert "ensure_heartbeat_schedule" in method_names
    assert any(
        c[0] == "attach_session" and c[1] == ("pollypm",)
        for c in sup.tmux.calls
    )
    assert rail_daemon_calls == [Path("/tmp/pollypm.toml")]
    assert isinstance(result, ExecutionResult)
    assert LaunchAction.BOOTSTRAP_LAUNCHES in result.actions_run
    assert LaunchAction.ATTACH_SESSION in result.actions_run


def test_first_launch_bootstrap_failure_returns_nonzero_exit() -> None:
    """If bootstrap_tmux raises, the executor records the failure
    via exit_code without crashing the run. cli.up() can then
    convert exit_code to typer.Exit."""
    probe = _probe()
    plan = plan_launch(probe)
    sup = _FakeSupervisor(bootstrap_raises=RuntimeError("no controllers"))
    exe, _ = _executor(probe=probe, supervisor=sup)
    result = exe.execute(plan)
    assert result.exit_code is not None and result.exit_code != 0


# ---------------------------------------------------------------------------
# ATTACH_EXISTING
# ---------------------------------------------------------------------------


def test_attach_existing_no_bootstrap_no_respawn() -> None:
    """Healthy cockpit: ensure_console_window + attach. Must NOT
    call bootstrap_tmux or respawn anything (#841)."""
    probe = _probe(main_session_alive=True, closet_session_alive=True)
    plan = plan_launch(probe)
    assert plan.state is LaunchState.ATTACH_EXISTING

    exe, sup = _executor(probe=probe)
    result = exe.execute(plan)

    method_names = [c[0] for c in sup.calls]
    assert "bootstrap_tmux" not in method_names
    assert "ensure_console_window" in method_names
    assert any(c[0] == "attach_session" for c in sup.tmux.calls)
    assert LaunchAction.RESPAWN_RAIL not in result.actions_run
    assert LaunchAction.RESPAWN_SHELL not in result.actions_run


def test_attach_prepares_cockpit_before_blocking_attach() -> None:
    """Outside-tmux attach must not happen until the cockpit layout/TUI is ready."""
    probe = _probe(main_session_alive=True, closet_session_alive=True)
    plan = plan_launch(probe)
    order: list[str] = []
    exe, sup = _executor(
        probe=probe,
        before_attach=lambda: order.append("prepare"),
    )
    original_attach = sup.tmux.attach_session

    def attach(name: str) -> int:
        order.append("attach")
        return original_attach(name)

    sup.tmux.attach_session = attach  # type: ignore[method-assign]

    exe.execute(plan)

    assert order == ["prepare", "attach"]


def test_attach_existing_inside_polly_uses_focus_not_attach() -> None:
    """Already inside the main session: focus, do not attach."""
    probe = _probe(
        main_session_alive=True,
        closet_session_alive=True,
        current_tmux_session="pollypm",
    )
    plan = plan_launch(probe)
    exe, sup = _executor(probe=probe)
    result = exe.execute(plan)

    assert "focus_console" in [c[0] for c in sup.calls]
    assert not any(c[0] == "attach_session" for c in sup.tmux.calls)
    assert LaunchAction.FOCUS_CONSOLE in result.actions_run


def test_attach_existing_inside_unrelated_tmux_switches() -> None:
    """tmux switch-client across unrelated sessions is the
    canonical handoff (#884 corrected the false-positive
    UNSUPPORTED rule)."""
    probe = _probe(
        main_session_alive=True,
        closet_session_alive=True,
        current_tmux_session="otherwork",
    )
    plan = plan_launch(probe)
    exe, sup = _executor(probe=probe)
    result = exe.execute(plan)

    assert any(
        c[0] == "switch_client" and c[1] == ("pollypm",)
        for c in sup.tmux.calls
    )
    assert LaunchAction.SWITCH_CLIENT in result.actions_run


# ---------------------------------------------------------------------------
# RESTORE_FROM_CLOSET
# ---------------------------------------------------------------------------


def test_restore_from_closet_recreates_main_session() -> None:
    """Closet alive, main gone: ENSURE_MAIN_SESSION must call
    create_session + the standard window options. Must NOT
    bootstrap (closet is alive)."""
    probe = _probe(closet_session_alive=True)
    plan = plan_launch(probe)
    assert plan.state is LaunchState.RESTORE_FROM_CLOSET

    exe, sup = _executor(probe=probe)
    result = exe.execute(plan)

    tmux_methods = [c[0] for c in sup.tmux.calls]
    sup_methods = [c[0] for c in sup.calls]
    assert "create_session" in tmux_methods
    assert "set_window_option" in tmux_methods
    assert "bootstrap_tmux" not in sup_methods
    assert LaunchAction.ENSURE_MAIN_SESSION in result.actions_run


# ---------------------------------------------------------------------------
# RECOVER_DEAD_SHELL / RECOVER_DEAD_RAIL
# ---------------------------------------------------------------------------


def test_recover_dead_shell_runs_shell_respawn() -> None:
    """Dead console pane: RESPAWN_SHELL action runs (delegates to
    ensure_console_window today; the action being named in the
    run record is the observability hook)."""
    probe = _probe(
        main_session_alive=True,
        closet_session_alive=True,
        console_pane_alive=False,
    )
    plan = plan_launch(probe)
    assert plan.state is LaunchState.RECOVER_DEAD_SHELL

    exe, _ = _executor(probe=probe)
    result = exe.execute(plan)
    assert LaunchAction.RESPAWN_SHELL in result.actions_run
    assert LaunchAction.RESPAWN_RAIL not in result.actions_run


def test_recover_dead_rail_runs_rail_respawn() -> None:
    """Dead rail pane: RESPAWN_RAIL fires."""
    probe = _probe(
        main_session_alive=True,
        closet_session_alive=True,
        console_pane_alive=True,
        rail_pane_alive=False,
        rail_pane_running_non_shell=False,
    )
    plan = plan_launch(probe)
    assert plan.state is LaunchState.RECOVER_DEAD_RAIL

    exe, _ = _executor(probe=probe)
    result = exe.execute(plan)
    assert LaunchAction.RESPAWN_RAIL in result.actions_run
    assert LaunchAction.RESPAWN_SHELL not in result.actions_run


# ---------------------------------------------------------------------------
# RECOVER_MISSING_CLOSET
# ---------------------------------------------------------------------------


def test_recover_missing_closet_calls_bootstrap() -> None:
    """Main alive but closet gone: bootstrap_tmux re-creates the
    closet (the canonical reconciliation path)."""
    probe = _probe(main_session_alive=True, closet_session_alive=False)
    plan = plan_launch(probe)
    assert plan.state is LaunchState.RECOVER_MISSING_CLOSET

    exe, sup = _executor(probe=probe)
    result = exe.execute(plan)

    method_names = [c[0] for c in sup.calls]
    assert "bootstrap_tmux" in method_names
    assert LaunchAction.BOOTSTRAP_LAUNCHES in result.actions_run


# ---------------------------------------------------------------------------
# UPGRADE_RESTART
# ---------------------------------------------------------------------------


def test_upgrade_restart_no_bootstrap() -> None:
    """Upgrade marker + main alive: controlled relaunch without
    re-bootstrap. ensure_console_window + heartbeat + daemon +
    attach is the canonical sequence."""
    probe = _probe(
        main_session_alive=True,
        closet_session_alive=True,
        upgrade_marker_present=True,
    )
    plan = plan_launch(probe)
    assert plan.state is LaunchState.UPGRADE_RESTART

    rail_daemon_calls: list[Path] = []
    exe, sup = _executor(probe=probe, rail_daemon_calls=rail_daemon_calls)
    result = exe.execute(plan)

    method_names = [c[0] for c in sup.calls]
    assert "bootstrap_tmux" not in method_names
    assert "ensure_console_window" in method_names
    assert "ensure_heartbeat_schedule" in method_names
    assert rail_daemon_calls == [Path("/tmp/pollypm.toml")]


# ---------------------------------------------------------------------------
# UNSUPPORTED — defensive behavior
# ---------------------------------------------------------------------------


def test_unsupported_returns_fail_closed_without_mutation() -> None:
    """If UNSUPPORTED leaks past the caller's gate, the executor
    refuses to mutate tmux."""
    plan = LaunchPlan(
        state=LaunchState.UNSUPPORTED,
        context=LaunchContext.OUTSIDE_TMUX,
        actions=(LaunchAction.FAIL_CLOSED,),
        reason="missing config",
    )
    probe = _probe()
    exe, sup = _executor(probe=probe)
    result = exe.execute(plan)

    assert result.exit_code is not None and result.exit_code != 0
    # No mutating supervisor or tmux call fired.
    sup_method_names = [c[0] for c in sup.calls]
    tmux_method_names = [c[0] for c in sup.tmux.calls]
    assert "bootstrap_tmux" not in sup_method_names
    assert "create_session" not in tmux_method_names
    assert "attach_session" not in tmux_method_names


# ---------------------------------------------------------------------------
# Handler exception isolation
# ---------------------------------------------------------------------------


def test_handler_exception_does_not_crash_run(monkeypatch) -> None:
    """A buggy handler must not stop the executor."""
    probe = _probe(main_session_alive=True, closet_session_alive=True)
    plan = plan_launch(probe)
    exe, sup = _executor(probe=probe)

    # Force the ensure_console_window handler to raise.
    def _explode(_self):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        LaunchPlanExecutor,
        "_on_ensure_console_window",
        _explode,
    )
    result = exe.execute(plan)
    # The run continues to ATTACH despite the broken handler.
    assert any(c[0] == "attach_session" for c in sup.tmux.calls)


# ---------------------------------------------------------------------------
# Action coverage
# ---------------------------------------------------------------------------


def test_executor_dispatch_table_covers_every_launch_action() -> None:
    """Every :class:`LaunchAction` must have a handler. A new
    action added to the enum without an executor entry is a
    silent gap — the audit explicitly named this risk."""
    probe = _probe()
    exe, _ = _executor(probe=probe)
    handled = set(exe._dispatch.keys())
    for action in LaunchAction:
        assert action in handled, f"unhandled LaunchAction: {action!r}"
