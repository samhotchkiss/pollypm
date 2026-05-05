import os
import subprocess
import sys
from pathlib import Path

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
