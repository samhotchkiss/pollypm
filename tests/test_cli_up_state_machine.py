"""Tests for the cli.up() ↔ launch-state-machine wiring (#884).

The launch-state machine became the named source of truth for
the ``pm up`` decision logic. These tests assert that ``up()``
actually consults it: it echoes the named state, refuses
UNSUPPORTED with the plan's actionable reason, and routes the
existing supervisor calls without disturbing legacy behavior.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pollypm import cli


# ---------------------------------------------------------------------------
# Probe builder
# ---------------------------------------------------------------------------


def test_probe_builder_derives_closet_name_from_main_name() -> None:
    """When the supervisor mock omits ``storage_closet_session_name``,
    the probe builder must fall back to the canonical
    ``<main>-storage-closet`` convention so the state machine
    has a non-empty closet name to reason about."""

    class _FakeTmux:
        def has_session(self, _name: str) -> bool:
            return False

        def current_session_name(self) -> None:
            return None

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project", (), {"tmux_session": "pollypm"}
                    )()
                },
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    assert probe.main_session_name == "pollypm"
    assert probe.closet_session_name == "pollypm-storage-closet"


def test_probe_builder_swallows_tmux_errors() -> None:
    """The probe must never raise; ``has_session`` failures fall
    back to ``False``."""

    class _BrokenTmux:
        def has_session(self, _name: str) -> bool:
            raise RuntimeError("tmux exploded")

        def current_session_name(self) -> None:
            return None

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _BrokenTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project", (), {"tmux_session": "pollypm"}
                    )()
                },
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    assert probe.main_session_alive is False
    assert probe.closet_session_alive is False


# ---------------------------------------------------------------------------
# State name in CLI output
# ---------------------------------------------------------------------------


def test_up_echoes_named_launch_state(monkeypatch, tmp_path: Path) -> None:
    """``pm up`` must echo the canonical state name + reason from
    the state machine. The named log line is the audit's
    observability foothold (#884) — every launch decision now
    carries a stable identifier the user (and tests) can grep."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            # Both main and closet are alive — the ATTACH_EXISTING
            # happy path.
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> str:
            return "pollypm"

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project", (), {"tmux_session": "pollypm"}
                    )()
                },
            )()

        def ensure_layout(self) -> None:
            return None

        def ensure_console_window(self) -> None:
            return None

        def ensure_heartbeat_schedule(self) -> None:
            return None

        def focus_console(self) -> None:
            return None

        def start_cockpit_tui(self, _session_name: str) -> None:
            # #1075 — fakes must honour the cockpit hook surface so the
            # before_attach path is exercised without AttributeError.
            return None

    monkeypatch.setattr(cli, "_load_supervisor", lambda _path: _FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "[launch]" in result.output
    assert "attach_existing" in result.output


def test_up_refuses_unsupported_with_actionable_reason(
    monkeypatch, tmp_path: Path
) -> None:
    """When the state machine returns UNSUPPORTED (config missing
    ``project.tmux_session``), ``up()`` must refuse to mutate
    tmux and surface the plan's reason as the error message."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\n")

    class _FakeTmux:
        def has_session(self, _name: str) -> bool:
            return False

        def current_session_name(self) -> None:
            return None

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            # No tmux_session — the genuinely-unsupported case.
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": ""})()},
            )()

        def ensure_layout(self) -> None:
            return None

    monkeypatch.setattr(cli, "_load_supervisor", lambda _path: _FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code != 0
    # The named state must appear in the log line.
    assert "unsupported" in result.output.lower()
    # The actionable reason must mention the missing config field.
    assert "tmux_session" in result.output or "names missing" in result.output


def test_up_unsupported_short_circuits_before_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    """#905 regression — when the launch plan is UNSUPPORTED,
    ``up()`` must refuse before triggering any startup side effect:
    no CoreRail.start(), no ensure_layout(), no transcript
    ingestion, no bootstrap_tmux."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\n")

    side_effects: list[str] = []

    class _FakeTmux:
        def has_session(self, _name: str) -> bool:
            return False

        def current_session_name(self) -> None:
            return None

    class _FakeCoreRail:
        def start(self) -> None:
            side_effects.append("core_rail.start")

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            # Empty tmux_session triggers UNSUPPORTED.
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project",
                        (),
                        {
                            "tmux_session": "",
                            "base_dir": tmp_path / ".pollypm",
                        },
                    )(),
                    "accounts": {},
                    "projects": {},
                },
            )()
            self.core_rail = _FakeCoreRail()

        def ensure_layout(self) -> None:
            side_effects.append("ensure_layout")

        def bootstrap_tmux(self, **_kwargs) -> str:
            side_effects.append("bootstrap_tmux")
            return ""

    monkeypatch.setattr(cli, "_load_supervisor", lambda _path: _FakeSupervisor())
    monkeypatch.setattr(
        cli,
        "start_transcript_ingestion",
        lambda _config: side_effects.append("transcript_ingest"),
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code != 0
    assert side_effects == [], (
        f"UNSUPPORTED must short-circuit before side effects; "
        f"observed: {side_effects}"
    )


def test_up_refuses_foreign_home_tmux_session_before_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    """#1179: a fresh HOME must not hijack another HOME's pollypm tmux session."""
    fresh_home = tmp_path / "fresh-home"
    fresh_home.mkdir()
    monkeypatch.setenv("HOME", str(fresh_home))
    config_path = fresh_home / ".pollypm" / "pollypm.toml"
    config_path.parent.mkdir()
    config_path.write_text("[project]\nname = \"pollypm\"\n")
    side_effects: list[str] = []

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name == "pollypm"

        def current_session_name(self) -> None:
            return None

        def show_environment(self, session_name: str, variable: str) -> str | None:
            assert session_name == "pollypm"
            assert variable == "HOME"
            return "/Users/sam"

    class _FakeCoreRail:
        def start(self) -> None:
            side_effects.append("core_rail.start")

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project",
                        (),
                        {
                            "tmux_session": "pollypm",
                            "base_dir": config_path.parent,
                        },
                    )(),
                    "accounts": {},
                    "projects": {},
                },
            )()
            self.core_rail = _FakeCoreRail()

        def ensure_layout(self) -> None:
            side_effects.append("ensure_layout")

        def start_cockpit_tui(self, _session_name: str) -> None:
            side_effects.append("start_cockpit_tui")

    monkeypatch.setattr(cli, "_load_supervisor", lambda _path: _FakeSupervisor())
    monkeypatch.setattr(
        cli,
        "start_transcript_ingestion",
        lambda _config: side_effects.append("transcript_ingest"),
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "tmux session 'pollypm' is in use by another HOME" in result.output
    assert "project.tmux_session" in result.output
    assert "before running `pm up`" in result.output
    assert side_effects == []


def test_root_pm_foreign_home_tmux_session_error_names_pm(
    monkeypatch, tmp_path: Path
) -> None:
    """#1227: bare ``pm`` should not report that the user ran ``pm up``."""
    fresh_home = tmp_path / "fresh-home"
    fresh_home.mkdir()
    monkeypatch.setenv("HOME", str(fresh_home))
    config_path = fresh_home / ".pollypm" / "pollypm.toml"
    config_path.parent.mkdir()
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name == "pollypm"

        def current_session_name(self) -> None:
            return None

        def show_environment(self, session_name: str, variable: str) -> str | None:
            assert session_name == "pollypm"
            assert variable == "HOME"
            return "/Users/sam"

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project",
                        (),
                        {
                            "tmux_session": "pollypm",
                            "base_dir": config_path.parent,
                        },
                    )(),
                    "accounts": {},
                    "projects": {},
                },
            )()

    monkeypatch.setattr(cli, "_load_supervisor", lambda _path: _FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["--config", str(config_path)])

    assert result.exit_code != 0
    assert "tmux session 'pollypm' is in use by another HOME" in result.output
    assert "before running `pm`" in result.output
    assert "before running `pm up`" not in result.output


def test_probe_detects_dead_console_pane() -> None:
    """#906 — when ``list_panes`` reports the console pane as dead,
    the probe must surface ``console_pane_alive=False`` so the
    state machine can route to RECOVER_DEAD_SHELL."""
    from pollypm.launch_state import LaunchState, plan_launch
    from pollypm.tmux.client import TmuxPane

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> None:
            return None

        def list_panes(self, _target: str) -> list[TmuxPane]:
            # Two panes: live rail TUI on the left, dead console on
            # the right. ``pane_left`` is "0" for left, "100" for right.
            return [
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="0",
                    pane_id="%1",
                    active=False,
                    pane_current_command="python",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left="0",
                    pane_width="40",
                ),
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="1",
                    pane_id="%2",
                    active=True,
                    pane_current_command="bash",
                    pane_current_path="/",
                    pane_dead=True,  # dead shell / right pane
                    pane_left="100",
                    pane_width="80",
                ),
            ]

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    assert probe.console_pane_alive is False
    assert probe.rail_pane_alive is True
    assert probe.rail_pane_running_non_shell is True

    plan = plan_launch(probe)
    assert plan.state is LaunchState.RECOVER_DEAD_SHELL


def test_probe_detects_dead_rail_pane() -> None:
    """#906 — dead rail pane (pane_dead=True or running a shell
    instead of the TUI) must route to RECOVER_DEAD_RAIL."""
    from pollypm.launch_state import LaunchState, plan_launch
    from pollypm.tmux.client import TmuxPane

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> None:
            return None

        def list_panes(self, _target: str) -> list[TmuxPane]:
            return [
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="0",
                    pane_id="%1",
                    active=True,
                    pane_current_command="python",
                    pane_current_path="/",
                    pane_dead=True,  # dead rail
                    pane_left="0",
                    pane_width="40",
                ),
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="1",
                    pane_id="%2",
                    active=False,
                    pane_current_command="bash",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left="100",
                    pane_width="80",
                ),
            ]

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    assert probe.console_pane_alive is True
    assert probe.rail_pane_alive is False
    assert probe.rail_pane_running_non_shell is False

    plan = plan_launch(probe)
    assert plan.state is LaunchState.RECOVER_DEAD_RAIL


def test_probe_treats_rail_running_shell_as_recoverable() -> None:
    """#906 + #841 — a rail pane running a shell (not the TUI)
    means the rail dropped back to its shell. The state machine
    refuses to respawn a *live* non-shell rail; here the rail is
    live but IS a shell, so RECOVER_DEAD_RAIL is the right state."""
    from pollypm.launch_state import plan_launch
    from pollypm.tmux.client import TmuxPane

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> None:
            return None

        def list_panes(self, _target: str) -> list[TmuxPane]:
            return [
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="0",
                    pane_id="%1",
                    active=True,
                    pane_current_command="bash",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left="0",
                    pane_width="40",
                ),
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="1",
                    pane_id="%2",
                    active=False,
                    # Rail dropped back to a shell.
                    pane_current_command="zsh",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left="100",
                    pane_width="80",
                ),
            ]

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    assert probe.rail_pane_alive is True
    assert probe.rail_pane_running_non_shell is False

    plan = plan_launch(probe)
    # Rail pane alive but running a shell means the TUI died back to
    # the shell prompt; attaching would strand the user in a broken
    # cockpit, so the planner must recover it before attach.
    assert plan.state.value in {"recover_dead_rail"}


def test_probe_treats_shell_wrapped_visible_cockpit_rail_as_healthy() -> None:
    """#1293 — tmux may report the rail pane's shell wrapper as
    ``pane_current_command`` while the cockpit rail UI is visibly
    running. The probe should trust that capture before emitting a
    recover_dead_rail diagnostic."""
    from pollypm.launch_state import LaunchAction, LaunchState, plan_launch
    from pollypm.tmux.client import TmuxPane

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> None:
            return None

        def list_panes(self, _target: str) -> list[TmuxPane]:
            return [
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="0",
                    pane_id="%1",
                    active=True,
                    pane_current_command="zsh",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left=0,
                    pane_width=30,
                ),
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="1",
                    pane_id="%2",
                    active=False,
                    pane_current_command="Python",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left=31,
                    pane_width=120,
                ),
            ]

        def capture_pane(self, target: str, lines: int = 200) -> str:
            assert target == "%1"
            assert lines == 40
            return """
     █▀█ █▀█ █   █   █▄█     │
     █▀▀ █▄█ █▄▄ █▄▄  █      │
        Plans first.         │
        Chaos later.         │
   ○ Home                    │
   ♡· Polly · chat           │
   ○ Workers (11)            │
   ◆ Inbox (42)              │
   ── projects ────────      │
"""

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    assert probe.rail_pane_alive is True
    assert probe.rail_pane_running_non_shell is True

    plan = plan_launch(probe)
    assert plan.state is LaunchState.ATTACH_EXISTING
    assert LaunchAction.RESPAWN_RAIL not in plan.actions


def test_probe_retries_transient_empty_rail_capture(monkeypatch) -> None:
    """#1293 — ``tmux capture-pane`` against the cockpit rail can
    return an empty / mid-redraw buffer when ``pm`` polls during a
    state transition. A single empty capture used to flip the rail
    classification to ``running_non_shell=False`` and route a healthy
    rail through ``RECOVER_DEAD_RAIL``, surfacing a false-positive
    diagnostic. The probe must retry on transient-empty captures
    before declaring the rail dead.

    Pins: first capture returns ``""`` (mid-redraw), second returns
    real cockpit content. Probe must trust the retry and emit
    ``ATTACH_EXISTING`` — not ``RECOVER_DEAD_RAIL``."""
    from pollypm.launch_state import LaunchAction, LaunchState, plan_launch
    from pollypm.tmux.client import TmuxPane

    # Capture call counter — first call returns empty, subsequent
    # calls return a real rail capture. This is the actual failure
    # mode the bug report describes (transient-empty / mid-redraw).
    capture_calls: list[str] = []

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> None:
            return None

        def list_panes(self, _target: str) -> list[TmuxPane]:
            return [
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="0",
                    pane_id="%1",
                    active=True,
                    pane_current_command="zsh",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left=0,
                    pane_width=30,
                ),
                TmuxPane(
                    session="pollypm",
                    window_index="0",
                    window_name="PollyPM",
                    pane_index="1",
                    pane_id="%2",
                    active=False,
                    pane_current_command="Python",
                    pane_current_path="/",
                    pane_dead=False,
                    pane_left=31,
                    pane_width=120,
                ),
            ]

        def capture_pane(self, target: str, lines: int = 200) -> str:
            capture_calls.append(target)
            # First call: empty (mid-redraw). Subsequent calls:
            # real cockpit content with all four required tokens.
            if len(capture_calls) == 1:
                return ""
            return """
     █▀█ █▀█ █   █   █▄█     │
     █▀▀ █▄█ █▄▄ █▄▄  █      │
        Plans first.         │
        Chaos later.         │
   ○ Home                    │
   ♡· Polly · chat           │
   ○ Workers (11)            │
   ◆ Inbox (42)              │
   ── projects ────────      │
"""

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    # Don't actually sleep during the test — the retry backoff is
    # an internal implementation detail. Patching ``time.sleep`` on
    # the cli module would shadow other call sites; the function
    # imports ``time`` locally so we patch the module reference.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    probe = cli._build_launch_probe(_FakeSupervisor())
    # Probe must trust the retry — rail is healthy, not dead.
    assert probe.rail_pane_alive is True
    assert probe.rail_pane_running_non_shell is True
    # And we must have actually retried — at least two captures.
    assert len(capture_calls) >= 2

    plan = plan_launch(probe)
    assert plan.state is LaunchState.ATTACH_EXISTING
    assert LaunchAction.RESPAWN_RAIL not in plan.actions


def test_pane_capture_retry_gives_up_on_persistent_empty() -> None:
    """The retry loop must terminate when captures are persistently
    empty. A genuinely dead rail (every retry empty) should still
    return False so the legit ``RECOVER_DEAD_RAIL`` path keeps
    working — the fix for #1293 must not hide real dead rails."""
    capture_calls: list[str] = []

    def _always_empty_capture(_pane_id: str, lines: int = 40) -> str:
        capture_calls.append(_pane_id)
        return ""

    class _Pane:
        pane_id = "%9"

    import time as _time

    # Don't burn wall clock on retry delays; we only assert the
    # function gave up and returned False.
    original_sleep = _time.sleep
    _time.sleep = lambda _s: None  # type: ignore[assignment]
    try:
        result = cli._pane_capture_looks_like_cockpit_rail(_Pane(), _always_empty_capture)
    finally:
        _time.sleep = original_sleep  # type: ignore[assignment]

    assert result is False
    # Retry loop must have actually retried, then given up.
    assert len(capture_calls) >= 2


def test_probe_falls_back_when_list_panes_unavailable() -> None:
    """#906 — when ``list_panes`` cannot enumerate (returns an
    empty list / raises), pane-liveness defaults to True. The
    fallback is documented; assuming live avoids the #841
    speculative-respawn class of bug."""
    from pollypm.launch_state import LaunchState, plan_launch

    class _BrokenListPanesTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> None:
            return None

        def list_panes(self, _target: str):
            raise RuntimeError("tmux unavailable")

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _BrokenListPanesTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    # Fallback: assumed alive.
    assert probe.console_pane_alive is True
    assert probe.rail_pane_alive is True
    assert probe.rail_pane_running_non_shell is True
    assert plan_launch(probe).state is LaunchState.ATTACH_EXISTING


def test_probe_plus_planner_attach_existing_no_typer() -> None:
    """End-to-end probe → planner check that bypasses Typer +
    monkeypatch + ``runner.invoke``.

    The full-suite ordering issue (#904) traced to the integration
    test relying on Typer state + the supervisor patch surviving
    teardown. This regression test calls the same composition
    (_build_launch_probe → plan_launch) directly so any future flake
    must surface here first — and at this layer, the inputs and
    outputs are pure values."""
    from pollypm.launch_state import LaunchState, plan_launch

    class _FakeTmux:
        def has_session(self, name: str) -> bool:
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self) -> str:
            return "pollypm"

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {"project": type("Project", (), {"tmux_session": "pollypm"})()},
            )()

    probe = cli._build_launch_probe(_FakeSupervisor())
    # Probe values are deterministic given the fake.
    assert probe.main_session_name == "pollypm"
    assert probe.closet_session_name == "pollypm-storage-closet"
    assert probe.main_session_alive is True
    assert probe.closet_session_alive is True
    assert probe.current_tmux_session == "pollypm"

    plan = plan_launch(probe)
    assert plan.state is LaunchState.ATTACH_EXISTING


def test_up_first_launch_state_named(monkeypatch, tmp_path: Path) -> None:
    """When neither main nor closet exists, the state machine
    must classify as FIRST_LAUNCH and ``up()`` must echo it."""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    class _FakeTmux:
        def has_session(self, _name: str) -> bool:
            return False

        def current_session_name(self) -> None:
            return None

    class _FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = _FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "project": type(
                        "Project", (), {"tmux_session": "pollypm"}
                    )()
                },
            )()

        def ensure_layout(self) -> None:
            return None

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def bootstrap_tmux(self, *, skip_probe: bool = False, on_status=None) -> str:
            # Refuse so the test asserts on the upstream state log
            # without driving the whole bootstrap path.
            raise RuntimeError("test-only short-circuit")

    monkeypatch.setattr(cli, "_load_supervisor", lambda _path: _FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert "[launch]" in result.output
    assert "first_launch" in result.output
