import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from pollypm.cockpit_rail import CockpitRouter


def _router(tmp_path: Path) -> CockpitRouter:
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    return CockpitRouter(config_path)


def test_right_pane_command_skips_uv_run_startup(tmp_path: Path) -> None:
    """#1208: right-pane routes must not pay ``uv run`` startup/sync cost."""

    router = _router(tmp_path)
    command = router._right_pane_command("inbox")

    assert "uv run pm" not in command
    assert sys.executable in command
    assert "-m pollypm cockpit-pane inbox" in command
    assert "-m pollypm.cli" not in command
    assert "PYTHONPATH=" in command


def test_right_pane_command_executes_cockpit_pane_help(tmp_path: Path) -> None:
    """The generated shell command must invoke a real Typer command."""

    router = _router(tmp_path)
    command = router._right_pane_command("--help")

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "POLLYPM_DISABLE_ERROR_NOTIFICATIONS": "1",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "cockpit-pane" in result.stdout


def test_package_main_fast_dispatches_inbox_pane(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """``python -m pollypm cockpit-pane inbox`` skips root CLI import."""

    import pollypm.cli_features.ui as ui
    import pollypm.cockpit_ui as cockpit_ui
    from pollypm import __main__ as package_main

    config_path = tmp_path / "pollypm.toml"
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(
        ui,
        "_enforce_migration_gate",
        lambda path: calls.append(("gate", path)),
    )
    monkeypatch.setattr(
        ui,
        "_install_cockpit_debug_log_handler",
        lambda path: calls.append(("log", path)),
    )

    class FakeInboxApp:
        def __init__(
            self,
            path: Path,
            *,
            initial_project: str | None = None,
        ) -> None:
            calls.append(("app", (path, initial_project)))

        def run(self, *, mouse: bool) -> None:
            calls.append(("run", mouse))

    monkeypatch.setattr(cockpit_ui, "PollyInboxApp", FakeInboxApp)

    handled = package_main._run_cockpit_pane_fast(
        [
            "cockpit-pane",
            "inbox",
            "--project",
            "demo",
            "--config",
            str(config_path),
        ]
    )

    assert handled is True
    assert ("gate", config_path) in calls
    assert ("log", config_path) in calls
    assert ("app", (config_path, "demo")) in calls
    assert ("run", True) in calls
