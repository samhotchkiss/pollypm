import json
import os
from pathlib import Path
import subprocess
import sys

from typer.testing import CliRunner

import pollypm.cli as cli


def test_root_command_defaults_to_up(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, Path] = {}
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    def fake_up(config_path: Path) -> None:
        called["config_path"] = config_path

    monkeypatch.setattr(cli, "up", fake_up)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["--config", str(config_path)])

    assert result.exit_code == 0
    assert called["config_path"] == config_path


def test_root_command_runs_onboarding_when_config_missing(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, Path] = {}

    def fake_first_run(config_path: Path) -> None:
        called["config_path"] = config_path

    def fail_up(config_path: Path) -> None:
        raise AssertionError("up should not run when config is missing")

    monkeypatch.setattr(cli, "_first_run_setup_and_launch", fake_first_run)
    monkeypatch.setattr(cli, "up", fail_up)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["--config", str(tmp_path / "pollypm.toml")])

    assert result.exit_code == 0
    assert called["config_path"] == tmp_path / "pollypm.toml"


def test_discover_config_path_returns_global_default(monkeypatch, tmp_path: Path) -> None:
    from pollypm.config import DEFAULT_CONFIG_PATH

    monkeypatch.chdir(tmp_path)

    resolved = cli._discover_config_path(DEFAULT_CONFIG_PATH)

    # Compare via .resolve() to handle macOS /tmp -> /private/tmp symlink
    # when HOME is under /tmp (agent-run isolated HOME); _discover_config_path
    # resolves symlinks whereas DEFAULT_CONFIG_PATH is built from the raw HOME.
    assert resolved.resolve() == DEFAULT_CONFIG_PATH.resolve()


def test_discover_config_path_returns_global_config(monkeypatch, tmp_path: Path) -> None:
    """resolve_config_path always returns the global config.

    Project-specific overrides are loaded separately via
    _merge_project_local_config, not by walking up the directory tree.
    """
    project_root = tmp_path / "repo"
    nested = project_root / "src" / "pkg"
    nested.mkdir(parents=True)
    # Even with a pollypm.toml in the project tree, the global path wins
    (project_root / "pollypm.toml").write_text("[project]\nname = \"PollyPM\"\n")

    monkeypatch.chdir(nested)

    resolved = cli._discover_config_path(cli.DEFAULT_CONFIG_PATH)

    assert resolved == cli.DEFAULT_CONFIG_PATH.resolve()


def test_status_default_config_uses_home_not_literal_tilde(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = """
import json
from typer.testing import CliRunner
import pollypm.cli as cli

result = CliRunner().invoke(cli.app, ["status"])
print(json.dumps({
    "exit_code": result.exit_code,
    "output": result.output,
    "exception": "" if result.exception is None else str(result.exception),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    expected_path = (tmp_path / ".pollypm" / "pollypm.toml").resolve()
    bad_path = (project_root / "~" / ".pollypm" / "pollypm.toml").resolve()
    combined = f"{payload['output']}\n{payload['exception']}"

    assert payload["exit_code"] != 0
    assert str(expected_path) in combined
    assert str(bad_path) not in combined


def test_importing_cli_defers_heavy_runtime_modules() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = """
import json
import sys
import pollypm.cli

payload = {
    "sqlalchemy": any(m == "sqlalchemy" or m.startswith("sqlalchemy.") for m in sys.modules),
    "plugin_host": "pollypm.plugin_host" in sys.modules,
    "service_api": any(m == "pollypm.service_api" or m.startswith("pollypm.service_api.") for m in sys.modules),
    "accounts": "pollypm.accounts" in sys.modules,
    "activity_panel": "pollypm.plugins_builtin.activity_feed.cockpit.feed_panel" in sys.modules,
    "activity_plugin": "pollypm.plugins_builtin.activity_feed.plugin" in sys.modules,
    "activity_projector": "pollypm.plugins_builtin.activity_feed.handlers.event_projector" in sys.modules,
    "jobs_workers": "pollypm.jobs.workers" in sys.modules,
    "memory_store": "pollypm.store.sqlalchemy_store" in sys.modules,
    "work_sqlite": "pollypm.work.sqlite_service" in sys.modules,
}
print(json.dumps(payload, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == {
        "accounts": False,
        "activity_panel": False,
        "activity_plugin": False,
        "activity_projector": False,
        "jobs_workers": False,
        "memory_store": False,
        "plugin_host": False,
        "service_api": False,
        "sqlalchemy": False,
        "work_sqlite": False,
    }


def test_root_command_attaches_existing_session_when_default_config_missing(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    class FakeTmux:
        def current_session_name(self):
            return None

        def has_session(self, name: str) -> bool:
            return name == "pollypm"

        def attach_session(self, name: str) -> int:
            called["attached"] = name
            return 0

    fake_default = tmp_path / "no-such-dir" / "pollypm.toml"
    monkeypatch.setattr(cli, "DEFAULT_CONFIG_PATH", fake_default)
    monkeypatch.setattr(cli, "_discover_config_path", lambda p: fake_default)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "current_session_name", lambda: None)
    monkeypatch.setattr(cli, "probe_session", lambda name: name == "pollypm")
    monkeypatch.setattr(cli, "attach_existing_session", lambda name: (called.update({"attached": name}) or 0))
    monkeypatch.setattr(cli, "_first_run_setup_and_launch", lambda config_path: (_ for _ in ()).throw(AssertionError("should not onboard")))

    runner = CliRunner()
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0
    assert called["attached"] == "pollypm"


def test_up_surfaces_bootstrap_failure_cleanly(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    class FakeTmux:
        def has_session(self, name: str) -> bool:
            return False

        def current_session_name(self):
            return None

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type("Config", (), {"project": type("Project", (), {"tmux_session": "pollypm"})()})()

        def ensure_layout(self) -> None:
            return None

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def bootstrap_tmux(self, *, skip_probe: bool = False, on_status=None) -> str:
            raise RuntimeError("PollyPM could not launch any controller account: claude_demo: probe failed")

        def start_cockpit_tui(self, _session_name: str) -> None:
            # #1085 — fakes must honour the cockpit hook surface so the
            # before_attach path is exercised without AttributeError.
            return None

    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "PollyPM could not launch any controller account" in result.output


def test_up_ensures_heartbeat_schedule_for_existing_session(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")
    calls: list[str] = []

    class FakeTmux:
        def has_session(self, name: str) -> bool:
            # Both main and storage closet alive — the production
            # ATTACH_EXISTING shape. Pre-#884 the state machine
            # only checked main; now it also confirms the closet
            # is up so RECOVER_MISSING_CLOSET fires when the
            # closet really is gone.
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self):
            return "pollypm"

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type("Config", (), {"project": type("Project", (), {"tmux_session": "pollypm"})()})()

        def ensure_layout(self) -> None:
            return None

        def ensure_console_window(self) -> None:
            calls.append("console")

        def ensure_heartbeat_schedule(self) -> None:
            calls.append("heartbeat")

        def focus_console(self) -> None:
            calls.append("focus")

        def start_cockpit_tui(self, _session_name: str) -> None:
            # #1085 — fakes must honour the cockpit hook surface so the
            # before_attach path is exercised without AttributeError.
            return None

    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == ["console", "heartbeat", "focus"]
    assert "Already inside tmux session pollypm" in result.output


def test_up_switches_current_tmux_client_to_polly_session(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")
    calls: list[object] = []

    class FakeTmux:
        def has_session(self, name: str) -> bool:
            # Both main and storage closet alive — the production
            # ATTACH_EXISTING shape (#884 / #896 wiring).
            return name in {"pollypm", "pollypm-storage-closet"}

        def current_session_name(self):
            return "scratch"

        def switch_client(self, name: str) -> int:
            calls.append(("switch", name))
            return 0

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type("Config", (), {"project": type("Project", (), {"tmux_session": "pollypm"})()})()

        def ensure_layout(self) -> None:
            return None

        def ensure_console_window(self) -> None:
            calls.append("console")

        def ensure_heartbeat_schedule(self) -> None:
            calls.append("heartbeat")

        def focus_console(self) -> None:
            calls.append("focus")

        def start_cockpit_tui(self, _session_name: str) -> None:
            # #1085 — fakes must honour the cockpit hook surface so the
            # before_attach path is exercised without AttributeError.
            return None

    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())

    runner = CliRunner()
    result = runner.invoke(cli.app, ["up", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == ["console", "heartbeat", "focus", ("switch", "pollypm")]
    assert "Switching to tmux session pollypm" in result.output
    assert "tmux switch-client" not in result.output


def test_require_pollypm_session_allows_storage_closet() -> None:
    class FakeTmux:
        def current_session_name(self):
            return "pollypm-storage-closet"

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type("Config", (), {"project": type("Project", (), {"tmux_session": "pollypm"})()})()

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

    cli._require_pollypm_session(FakeSupervisor())


def test_worker_start_no_role_is_deprecated(monkeypatch, tmp_path: Path) -> None:
    """``pm worker-start <project>`` without ``--role`` exits with code 2.

    The managed-worker pattern was replaced by per-task workers (via
    ``pm task claim``); managed ``worker-<project>`` sessions leaked
    memory because they outlived their tasks with no cleanup hook.
    The CLI rejects the call with a three-question-rule banner that
    points at the supported flows.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["worker-start", "pollypm", "--prompt", "Do the next task",
         "--config", str(config_path)],
    )
    assert result.exit_code == 2
    assert "deprecated" in result.output
    assert "pm task claim" in result.output


def test_shortcuts_command_prints_curated_cheatsheet() -> None:
    runner = CliRunner()

    result = runner.invoke(cli.app, ["shortcuts"])

    assert result.exit_code == 0
    assert "PollyPM shortcuts" in result.output
    assert "Create" in result.output
    assert "pm task create" in result.output
    assert "pm activity --follow" in result.output
    assert "pm briefing" in result.output


def test_worker_start_role_architect_still_works(monkeypatch, tmp_path: Path) -> None:
    """``pm worker-start --role architect <project>`` remains supported.

    Architects are long-lived per-project sessions (the planner lane)
    that the architect 2-hour idle-close mechanism manages. Only the
    default ``--role worker`` path was deprecated; the architect path
    is still the canonical way to spawn the planner.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")
    created: list[tuple[Path, str, str | None]] = []
    launched: list[tuple[Path, str]] = []

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = type(
                "Config",
                (),
                {
                    "sessions": {},
                    "project": type("Project", (), {"tmux_session": "pollypm"})(),
                },
            )()

        def tmux_session_for_launch(self, launch) -> str:
            return "pollypm-storage-closet"

        def plan_launches(self):
            session = type("Session", (), {"name": "architect_pollypm"})()
            return [type("Launch", (), {"session": session, "window_name": "architect-pollypm"})()]

    monkeypatch.setattr(cli, "_require_pollypm_session", lambda supervisor: None)
    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())
    monkeypatch.setattr(
        cli,
        "create_worker_session",
        lambda path, project_key, prompt=None, role="worker", agent_profile=None: (
            created.append((path, project_key, prompt))
            or type("Session", (), {"name": "architect_pollypm"})()
        ),
    )
    monkeypatch.setattr(cli, "launch_worker_session", lambda path, session_name: launched.append((path, session_name)))

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["worker-start", "pollypm", "--role", "architect",
         "--prompt", "Plan it", "--config", str(config_path)],
    )
    assert result.exit_code == 0
    assert created == [(config_path, "pollypm", "Plan it")]
    assert launched == [(config_path, "architect_pollypm")]
    assert "Managed architect architect_pollypm ready for project pollypm" in result.output


def test_worker_start_role_architect_works_outside_tmux(monkeypatch, tmp_path: Path) -> None:
    """Regression for #1055: ``pm worker-start --role architect <project>``
    succeeds from a non-tmux shell.

    ``no_session`` alerts emit the hint ``Try: pm worker-start --role
    architect <project>`` for users to copy-paste straight into a
    regular terminal while triaging. Before #1055 the command bailed
    with ``This command must run inside tmux session 'pollypm'`` because
    a defensive ``_require_pollypm_session`` gate ran first — which is
    wrong, because the actual spawn uses ``tmux new-window -t
    pollypm-storage-closet:...`` (targets a session by name, not the
    current session). This test pins the post-fix behavior: the gate
    is NOT consulted, and the command completes normally with the
    caller's tmux state set to "no current session" (i.e. ``$TMUX``
    unset and ``current_session_name`` returning ``None``). If a future
    change re-introduces the gate, this test fails fast.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")
    created: list[tuple[Path, str, str | None, str, str | None]] = []
    launched: list[tuple[Path, str]] = []

    class FakeTmux:
        def current_session_name(self):
            # Caller is NOT in any tmux session — the bug case from #1055.
            return None

    class FakeSupervisor:
        def __init__(self) -> None:
            self.tmux = FakeTmux()
            self.config = type(
                "Config",
                (),
                {
                    "sessions": {},
                    "project": type("Project", (), {"tmux_session": "pollypm"})(),
                },
            )()

        def storage_closet_session_name(self) -> str:
            return "pollypm-storage-closet"

        def tmux_session_for_launch(self, launch) -> str:
            return "pollypm-storage-closet"

        def plan_launches(self):
            session = type("Session", (), {"name": "architect_pollypm"})()
            return [type("Launch", (), {"session": session, "window_name": "architect-pollypm"})()]

    # Note: we deliberately do NOT monkeypatch ``_require_pollypm_session``
    # here. The whole point of the test is that the gate is gone for
    # ``worker-start``, so the command must succeed even when the fake
    # supervisor reports "no current tmux session".
    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())
    monkeypatch.setattr(
        cli,
        "create_worker_session",
        lambda path, project_key, prompt=None, role="worker", agent_profile=None: (
            created.append((path, project_key, prompt, role, agent_profile))
            or type("Session", (), {"name": "architect_pollypm"})()
        ),
    )
    monkeypatch.setattr(
        cli,
        "launch_worker_session",
        lambda path, session_name: launched.append((path, session_name)),
    )
    # Belt-and-braces: the underlying ``current_session_name`` reads
    # ``$TMUX`` in some implementations. Strip it so we are unambiguously
    # outside tmux from the OS's perspective too.
    monkeypatch.delenv("TMUX", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["worker-start", "pollypm", "--role", "architect",
         "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    # The pre-fix failure mode: BadParameter raised by the gate. Pin
    # the absence of that exact phrasing so a regression surfaces with
    # an obvious message rather than a generic "exit_code != 0".
    assert "must run inside tmux session" not in result.output
    assert created == [(config_path, "pollypm", None, "architect", None)]
    assert launched == [(config_path, "architect_pollypm")]
    assert "Managed architect architect_pollypm ready for project pollypm" in result.output


def test_help_lists_heartbeat_agent_commands() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "status" in result.output
    assert "send" in result.output
    assert "alert" in result.output
    assert "session" in result.output
    assert "heartbeat" in result.output
