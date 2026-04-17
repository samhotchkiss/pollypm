"""Tests for ``pm project new`` drift-aware routing (issue #274).

Covers ``_classify_project_state`` + the routing in ``new_cmd``. A
fresh directory should land on the cold-start planner task; a
directory with commits, a prior ``docs/plan/plan.md``, pre-existing
source files, or prior work_tasks rows should land on the replan
task instead. ``--skip-planner`` still suppresses both paths, and
``--force-cold-start`` overrides classification.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from pollypm.config import write_config
from pollypm.models import (
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
)
from pollypm.plugins_builtin.project_planning.cli import project as project_cli
from pollypm.plugins_builtin.project_planning.cli.project import project_app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(path: Path, *, commits: int = 0) -> None:
    """Initialise a real git repo at ``path`` with ``commits`` commits."""
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.test"], cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(path), check=True,
    )
    for i in range(commits):
        marker = path / f"c{i}.txt"
        marker.write_text(f"commit {i}\n")
        subprocess.run(
            ["git", "add", marker.name], cwd=str(path), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", f"c{i}"],
            cwd=str(path), check=True,
        )


def _make_project_repo(tmp_path: Path, name: str = "demo") -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / ".git").mkdir()
    return path


def _write_minimal_config(
    tmp_path: Path, *, projects: dict[str, Path] | None = None,
) -> Path:
    projects = projects or {}
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account=""),
        accounts={},
        sessions={},
        projects={
            key: KnownProject(
                key=key,
                path=path,
                name=key,
                persona_name="",
                kind=ProjectKind.FOLDER,
            )
            for key, path in projects.items()
        },
    )
    config_path = tmp_path / "pollypm.toml"
    write_config(config, config_path, force=True)
    return config_path


def _only_plan_task(repo: Path, project_key: str):
    """Return the single plan_project task on ``repo``, failing clearly."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = repo / ".pollypm" / "state.db"
    assert db_path.exists(), "auto-fire should have opened the work DB"
    with SQLiteWorkService(db_path=db_path, project_path=repo) as svc:
        tasks = [
            t for t in svc.list_tasks(project=project_key)
            if t.flow_template_id == "plan_project"
        ]
    assert len(tasks) == 1, f"expected exactly 1 plan_project task, got {tasks}"
    return tasks[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProjectNewReplanRouting:
    def test_fresh_dir_classifies_greenfield_cold_start(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Fresh git init, no commits → greenfield → cold-start task."""
        repo = tmp_path / "fresh"
        repo.mkdir()
        _git_init(repo, commits=0)
        config_path = _write_minimal_config(tmp_path)

        # Sanity-check the classifier directly.
        assert project_cli._classify_project_state(repo) == "greenfield"

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire"),
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Fresh project" in result.stdout
        assert "cold-start planner" in result.stdout
        # Auto-fired task is a plan (not replan).
        assert "Auto-created plan_project task" in result.stdout
        task = _only_plan_task(repo, "fresh")
        assert task.title.startswith("Plan project")
        assert "Re-run" not in (task.description or "")

    def test_existing_commits_classifies_existing_replan(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """> 1 git commit → existing → drift-aware replan task."""
        repo = tmp_path / "existing"
        repo.mkdir()
        _git_init(repo, commits=3)
        config_path = _write_minimal_config(tmp_path)

        assert project_cli._classify_project_state(repo) == "existing"

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire"),
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Detected existing project" in result.stdout
        assert "drift-aware replan" in result.stdout
        assert "Auto-created replan task" in result.stdout
        task = _only_plan_task(repo, "existing")
        assert task.title.startswith("Replan project")
        assert "drift" in (task.description or "").lower()

    def test_existing_plan_md_classifies_existing_replan(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``docs/plan/plan.md`` present → existing → replan task."""
        repo = tmp_path / "planned"
        repo.mkdir()
        (repo / ".git").mkdir()  # no commits, but plan.md triggers existing.
        (repo / "docs" / "plan").mkdir(parents=True)
        (repo / "docs" / "plan" / "plan.md").write_text(
            "# Old plan\n\nSome prior decisions.\n",
        )
        config_path = _write_minimal_config(tmp_path)

        assert project_cli._classify_project_state(repo) == "existing"

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire"),
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Detected existing project" in result.stdout
        task = _only_plan_task(repo, "planned")
        assert task.title.startswith("Replan project")

    def test_existing_py_files_classifies_existing_replan(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Any ``*.py`` file in the tree → existing → replan."""
        repo = tmp_path / "pycode"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "main.py").write_text("print('hi')\n")
        config_path = _write_minimal_config(tmp_path)

        assert project_cli._classify_project_state(repo) == "existing"

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire"),
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Detected existing project" in result.stdout
        task = _only_plan_task(repo, "pycode")
        assert task.title.startswith("Replan project")

    def test_skip_planner_suppresses_both_paths(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``--skip-planner`` on an existing project still skips everything."""
        repo = tmp_path / "skipme"
        repo.mkdir()
        _git_init(repo, commits=3)
        (repo / "main.py").write_text("x = 1\n")
        config_path = _write_minimal_config(tmp_path)

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire with --skip-planner"),
            ),
        )

        result = runner.invoke(
            project_app,
            [
                "new", str(repo), "--skip-planner",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Skipped planner" in result.stdout
        assert "Auto-created" not in result.stdout
        assert "Detected existing project" not in result.stdout
        assert "Fresh project" not in result.stdout
        # No plan task (no DB at all).
        assert not (repo / ".pollypm" / "state.db").exists()

    def test_force_cold_start_overrides_existing_classification(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """``--force-cold-start`` on an existing project forces cold-start."""
        repo = tmp_path / "override"
        repo.mkdir()
        _git_init(repo, commits=3)
        (repo / "main.py").write_text("x = 1\n")
        config_path = _write_minimal_config(tmp_path)

        assert project_cli._classify_project_state(repo) == "existing"

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire"),
            ),
        )

        result = runner.invoke(
            project_app,
            [
                "new", str(repo), "--force-cold-start",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Fresh project" in result.stdout
        assert "Detected existing project" not in result.stdout
        assert "Auto-created plan_project task" in result.stdout
        task = _only_plan_task(repo, "override")
        assert task.title.startswith("Plan project")
        assert "drift" not in (task.description or "").lower()
