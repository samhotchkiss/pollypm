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
