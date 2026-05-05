import builtins
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from types import SimpleNamespace

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
    capsys,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """``python -m pollypm cockpit-pane inbox`` skips root CLI import."""

    import pollypm.cockpit_ui as cockpit_ui
    import pollypm.cli_features.ui as ui
    import pollypm.config as config_mod
    import pollypm.cockpit_inbox_items as inbox_items
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
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda path: SimpleNamespace(projects={"demo": object()}),
    )
    monkeypatch.setattr(
        inbox_items,
        "load_inbox_entries",
        lambda config: (
            [
                SimpleNamespace(
                    task_id="demo/1",
                    title="Plan ready for review",
                    project="demo",
                    triage_label="plan review",
                    needs_action=True,
                    is_orphaned=False,
                ),
                SimpleNamespace(
                    task_id="other/1",
                    title="Other project",
                    project="other",
                    triage_label="plan review",
                    needs_action=True,
                    is_orphaned=False,
                ),
            ],
            {"demo/1"},
            {},
        ),
    )
    original_import = builtins.__import__
    first_package_import: list[tuple[str, str]] = []

    def tracking_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if (
            level == 0
            and not first_package_import
            and (name == "pollypm" or name.startswith("pollypm."))
        ):
            first_package_import.append((name, capsys.readouterr().out))
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracking_import)

    class FakeInboxApp:
        def __init__(
            self,
            path: Path,
            *,
            initial_project: str | None = None,
        ) -> None:
            out = capsys.readouterr().out
            assert "Inbox" in out
            assert "action needed" in out
            assert "Plan ready for review" in out
            assert "Other project" not in out
            assert "Loading action needed" not in out
            assert "Inbox is clear" not in out
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
    assert first_package_import
    assert first_package_import[0][0] == "pollypm.cli_features.ui"
    pre_import_output = first_package_import[0][1]
    assert "Inbox" in pre_import_output
    assert "Loading messages..." in pre_import_output
    assert "action needed" not in pre_import_output
    assert "Inbox is clear" not in pre_import_output
    assert ("gate", config_path) in calls
    assert ("log", config_path) in calls
    assert ("app", (config_path, "demo")) in calls
    assert ("run", True) in calls
