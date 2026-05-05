import sys
from pathlib import Path

from pollypm.cockpit_rail import CockpitRouter


def test_right_pane_command_skips_uv_run_startup(tmp_path: Path) -> None:
    """#1208: right-pane routes must not pay ``uv run`` startup/sync cost."""

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        f"[project]\nname = \"PollyPM\"\ntmux_session = \"pollypm\"\n"
        f"base_dir = \"{tmp_path / '.pollypm'}\"\n"
    )
    router = CockpitRouter(config_path)

    command = router._right_pane_command("inbox")

    assert "uv run pm" not in command
    assert sys.executable in command
    assert "-m pollypm.cli cockpit-pane inbox" in command
    assert "PYTHONPATH=" in command
