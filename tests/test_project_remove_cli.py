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
