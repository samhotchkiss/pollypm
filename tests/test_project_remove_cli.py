"""Tests for ``pm project remove`` (#1561).

Narrow scope: surface the existing ``remove_project`` core function as a
``pm project remove <key>`` CLI command. Mirrors the ``pm project new``
pattern. The command refuses when the project has queued or in-flight
work-service tasks unless ``--force`` (or ``--yes``) is supplied.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pollypm.plugins_builtin.project_planning.cli.project import project_app


runner = CliRunner()


def _write_config(
    config_path: Path,
    *,
    workspace_root: Path,
    project_path: Path,
    slug: str,
    extra_sessions: str = "",
) -> None:
    config_path.write_text(
        "[project]\n"
        'tmux_session = "pollypm-test"\n'
        f'workspace_root = "{workspace_root}"\n'
        "\n"
        f'[projects.{slug}]\n'
        f'key = "{slug}"\n'
        'name = "Demo"\n'
        f'path = "{project_path}"\n'
        f"{extra_sessions}"
    )


@pytest.fixture
def env(tmp_path: Path) -> dict:
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(
        config_path,
        workspace_root=workspace_root,
        project_path=project_path,
        slug="demo",
    )
    return {
        "config_path": config_path,
        "project_path": project_path,
        "workspace_root": workspace_root,
    }


def _load_cfg(config_path: Path):
    from pollypm.config import load_config
    return load_config(config_path)


# --------------------------------------------------------------------------
# Happy path: no active tasks → removes without prompting.
# --------------------------------------------------------------------------


def test_cli_remove_happy_path(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=0):
        result = runner.invoke(
            project_app,
            ["remove", "demo", "--config", str(env["config_path"])],
        )
    assert result.exit_code == 0, result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects


# --------------------------------------------------------------------------
# Unknown project → exit(1) with a clear error.
# --------------------------------------------------------------------------


def test_cli_remove_unknown_project_errors_cleanly(env) -> None:
    result = runner.invoke(
        project_app,
        ["remove", "does_not_exist", "--config", str(env["config_path"])],
    )
    assert result.exit_code == 1, result.output
    assert "Unknown project" in result.output


# --------------------------------------------------------------------------
# Active tasks: refuses without confirmation; --force bypasses.
# --------------------------------------------------------------------------


def test_cli_remove_aborts_with_active_tasks_and_no_force(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=3):
        # CliRunner sends an empty stdin → typer.confirm reads "" → False.
        result = runner.invoke(
            project_app,
            ["remove", "demo", "--config", str(env["config_path"])],
            input="n\n",
        )
    assert result.exit_code == 1, result.output
    assert "3 queued or in-flight tasks" in result.output
    assert "Aborted" in result.output

    # Config is untouched.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_cli_remove_force_bypasses_active_task_prompt(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=2):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--force",
                "--config", str(env["config_path"]),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Removed project 'demo'" in result.output
    # The follow-up note about orphan rows should fire when active>0.
    assert "left in place" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects


def test_cli_remove_yes_accepts_active_task_prompt(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=1):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--yes",
                "--config", str(env["config_path"]),
            ],
        )
    assert result.exit_code == 0, result.output
    # Singular noun agreement.
    assert "1 queued or in-flight task" in result.output

    config = _load_cfg(env["config_path"])
    assert "demo" not in config.projects


# --------------------------------------------------------------------------
# Session-reference refusal propagates from the core function.
# --------------------------------------------------------------------------


def test_cli_remove_refuses_when_project_still_has_sessions(tmp_path: Path) -> None:
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(
        config_path,
        workspace_root=workspace_root,
        project_path=project_path,
        slug="demo",
        extra_sessions=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
            "\n"
            "[accounts.claude_main]\n"
            'provider = "claude"\n'
            'home = "/tmp/claude_home"\n'
        ),
    )
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=0):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--force",
                "--config", str(config_path),
            ],
        )
    assert result.exit_code == 1, result.output
    assert "still used by session" in result.output

    config = _load_cfg(config_path)
    assert "demo" in config.projects


# --------------------------------------------------------------------------
# --purge-sessions cascade: kill tmux + drop [sessions.*] entries so the
# core function's session-reference invariant no longer refuses removal.
# --------------------------------------------------------------------------


def _project_with_sessions_config(
    tmp_path: Path, *, sessions_block: str
) -> tuple[Path, Path]:
    workspace_root = tmp_path / "dev"
    workspace_root.mkdir()
    project_path = workspace_root / "demo"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    config_path = tmp_path / "pollypm.toml"
    _write_config(
        config_path,
        workspace_root=workspace_root,
        project_path=project_path,
        slug="demo",
        extra_sessions=(
            sessions_block
            + "\n[accounts.claude_main]\n"
            'provider = "claude"\n'
            'home = "/tmp/claude_home"\n'
        ),
    )
    return config_path, project_path


def test_cli_remove_purge_sessions_kills_tmux_and_drops_entries(
    tmp_path: Path,
) -> None:
    """--purge-sessions tears down tmux + config so removal succeeds."""
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
            "\n"
            "[sessions.reviewer_demo]\n"
            'role = "reviewer"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "reviewer-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: True,
            "kill_session": lambda self, name: True,
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    tmux_target = (
        "pollypm.plugins_builtin.project_planning.cli.project.create_tmux_client"
    )
    # ``create_tmux_client`` is imported lazily inside
    # ``_purge_project_sessions``; patch the module the lazy import resolves
    # against (the source module) since the local symbol isn't bound at
    # import time of project.py.
    with (
        patch(count_target, return_value=0),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--purge-sessions",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Killed tmux session architect_demo" in result.output
    assert "Killed tmux session reviewer_demo" in result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(config_path)
    assert "demo" not in config.projects
    assert "architect_demo" not in config.sessions
    assert "reviewer_demo" not in config.sessions

    # Patch reference to silence unused warning when local stub is unused.
    _ = tmux_target


def test_cli_remove_purge_sessions_handles_already_dead_tmux(
    tmp_path: Path,
) -> None:
    """--purge-sessions still drops config entries when tmux session is gone."""
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: False,
            "kill_session": lambda self, name: False,
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with (
        patch(count_target, return_value=0),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--purge-sessions",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "tmux session was not running" in result.output
    assert "Removed project 'demo'" in result.output

    config = _load_cfg(config_path)
    assert "demo" not in config.projects
    assert "architect_demo" not in config.sessions


# --------------------------------------------------------------------------
# --dry-run: prints the teardown plan, mutates nothing.
# --------------------------------------------------------------------------


def test_cli_remove_dry_run_with_no_sessions(env) -> None:
    target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with patch(target, return_value=0):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--dry-run",
                "--config", str(env["config_path"]),
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Dry run: would remove project 'demo'" in result.output
    assert "sessions: (none)" in result.output
    assert "Re-run without --dry-run to apply." in result.output

    # Nothing mutated.
    config = _load_cfg(env["config_path"])
    assert "demo" in config.projects


def test_cli_remove_dry_run_lists_sessions_without_killing(tmp_path: Path) -> None:
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: True,
            # If this ever fires the dry-run guard is broken.
            "kill_session": lambda self, name: (_ for _ in ()).throw(
                AssertionError("kill_session must not run in --dry-run")
            ),
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with (
        patch(count_target, return_value=2),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--dry-run", "--purge-sessions",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "Dry run: would remove project 'demo'" in result.output
    assert "sessions to purge:" in result.output
    assert "architect_demo (live)" in result.output
    assert "2 queued/in-flight" in result.output

    # Nothing mutated.
    config = _load_cfg(config_path)
    assert "demo" in config.projects
    assert "architect_demo" in config.sessions


def test_cli_remove_dry_run_warns_when_sessions_present_without_purge(
    tmp_path: Path,
) -> None:
    config_path, _ = _project_with_sessions_config(
        tmp_path,
        sessions_block=(
            "[sessions.architect_demo]\n"
            'role = "architect"\n'
            'provider = "claude"\n'
            'account = "claude_main"\n'
            'cwd = "."\n'
            'project = "demo"\n'
            'window_name = "architect-demo"\n'
        ),
    )

    fake_tmux = type(
        "FakeTmux", (), {
            "has_session": lambda self, name: False,
            "kill_session": lambda self, name: False,
        },
    )()
    count_target = (
        "pollypm.plugins_builtin.project_planning.cli.project._count_active_tasks"
    )
    with (
        patch(count_target, return_value=0),
        patch(
            "pollypm.session_services.create_tmux_client",
            return_value=fake_tmux,
        ),
    ):
        result = runner.invoke(
            project_app,
            [
                "remove", "demo", "--dry-run",
                "--config", str(config_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "use --purge-sessions to tear down" in result.output
    assert "remove_project will refuse" in result.output
