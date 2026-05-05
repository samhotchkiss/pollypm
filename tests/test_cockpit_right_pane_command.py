import builtins
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
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


def test_route_inbox_skips_supervisor_context_before_respawn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Capital-I should reach tmux respawn without loading session plans."""

    router = _router(tmp_path)

    left = SimpleNamespace(
        pane_id="%1",
        pane_current_command="python",
        pane_left=0,
        pane_width=30,
        pane_dead=False,
    )
    right = SimpleNamespace(
        pane_id="%2",
        pane_current_command="python",
        pane_left=31,
        pane_width=120,
        pane_dead=False,
    )

    class FakeTmux:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def list_panes(self, target: str) -> list[Any]:
            assert target == "pollypm:PollyPM"
            return [left, right]

        def list_windows(self, name: str) -> list[Any]:
            self.calls.append(("list_windows", (name,)))
            return []

        def split_window(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("two-pane route should not split")

        def kill_pane(self, target: str) -> None:
            raise AssertionError(f"unexpected kill_pane {target}")

        def resize_pane_width(self, target: str, width: int) -> None:
            self.calls.append(("resize_pane_width", (target, width)))

        def respawn_pane(self, target: str, command: str) -> None:
            self.calls.append(("respawn_pane", (target, command)))

        def join_pane(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("unexpected join_pane")

        def break_pane(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("unexpected break_pane")

        def rename_window(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("unexpected rename_window")

        def swap_pane(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("unexpected swap_pane")

        def select_pane(self, target: str) -> None:
            self.calls.append(("select_pane", (target,)))

        def set_pane_history_limit(self, target: str, limit: int) -> None:
            self.calls.append(("set_pane_history_limit", (target, limit)))

        def run(self, *args: str, check: bool = True) -> None:
            self.calls.append(("run", (*args, check)))

    tmux = FakeTmux()
    router.tmux = tmux  # type: ignore[assignment]
    router._write_state(
        {
            "selected": "dashboard",
            "right_pane_id": "%2",
            "mounted_session": "operator",
            "mounted_identity": {"session_name": "operator"},
        }
    )
    monkeypatch.setattr(
        router,
        "_load_supervisor",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("inbox route loaded supervisor"),
        ),
    )
    monkeypatch.setattr(
        router,
        "_content_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("inbox route resolved content context"),
        ),
    )

    router.route_selected("inbox")

    respawns = [call for call in tmux.calls if call[0] == "respawn_pane"]
    assert len(respawns) == 1
    target, command = respawns[0][1]
    assert target == "%2"
    assert "-m pollypm cockpit-pane inbox" in command
    assert "uv run pm" not in command
    state = router._load_state()
    assert state["selected"] == "inbox"
    assert "mounted_session" not in state
    assert "mounted_identity" not in state


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
    assert first_package_import[0][0] == "pollypm.cockpit_inbox_items"
    pre_import_output = first_package_import[0][1]
    assert "Inbox" in pre_import_output
    assert "Loading messages..." in pre_import_output
    assert "action needed" not in pre_import_output
    assert "Inbox is clear" not in pre_import_output
    assert ("gate", config_path) in calls
    assert ("log", config_path) in calls
    assert ("app", (config_path, "demo")) in calls
    assert ("run", True) in calls
