"""Tests for ``pm project plan/replan/new`` CLI (pp10).

Exercises the CLI through ``typer.testing.CliRunner``. We drive against
a real ``SQLiteWorkService`` on a tmp project so ``pm project plan``
actually creates a task with ``flow=plan_project``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
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
# Fixtures
# ---------------------------------------------------------------------------


def _make_project_repo(tmp_path: Path, name: str = "demo") -> Path:
    """Create a minimal git-looking directory on disk."""
    path = tmp_path / name
    path.mkdir()
    (path / ".git").mkdir()
    return path


def _write_minimal_config(
    tmp_path: Path, *, projects: dict[str, Path] | None = None
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


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class TestPlan:
    def test_plan_creates_flow_plan_project_task(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["plan", "demo", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["project"] == "demo"
        assert payload["flow"] == "plan_project"
        assert payload["task_id"].startswith("demo/")
        # Work-service DB created at the expected location.
        assert (repo / ".pollypm" / "state.db").exists()

    def test_plan_text_output(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app, ["plan", "demo", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "planning task" in result.stdout
        assert "flow=plan_project" in result.stdout

    def test_plan_unknown_project_exits_nonzero(self, tmp_path: Path) -> None:
        config_path = _write_minimal_config(tmp_path)
        result = runner.invoke(
            project_app, ["plan", "nope", "--config", str(config_path)],
        )
        assert result.exit_code == 1
        assert "Unknown project" in result.stdout or "Unknown project" in result.stderr

    def test_plan_accepts_hyphenated_alias(self, tmp_path: Path) -> None:
        """Hyphens map to underscores — aligns with ``pm task`` conventions."""
        repo = _make_project_repo(tmp_path, name="my-demo")
        config_path = _write_minimal_config(
            tmp_path, projects={"my_demo": repo},
        )

        result = runner.invoke(
            project_app,
            ["plan", "my-demo", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["project"] == "my_demo"

    def test_plan_defaults_to_cwd_project(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        monkeypatch.chdir(repo)
        result = runner.invoke(
            project_app, ["plan", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["project"] == "demo"


# ---------------------------------------------------------------------------
# replan
# ---------------------------------------------------------------------------


class TestReplan:
    def test_replan_creates_task_with_mode(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["replan", "demo", "--config", str(config_path), "--json"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["mode"] == "replan"
        assert payload["flow"] == "plan_project"
        # Title prefix differs from plan — assert via the created task.
        from pollypm.work.sqlite_service import SQLiteWorkService
        with SQLiteWorkService(
            db_path=repo / ".pollypm" / "state.db",
            project_path=repo,
        ) as svc:
            task = svc.get(payload["task_id"])
            assert "Replan" in task.title


# ---------------------------------------------------------------------------
# new — register + prompt
# ---------------------------------------------------------------------------


class TestNew:
    def test_new_accepts_and_creates_plan_task(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Happy path — user accepts the planner prompt."""
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        # Force the prompt to return True (accept).
        monkeypatch.setattr(
            project_cli, "_prompt_run_planner", lambda default_yes=True: True,
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Registered project" in result.stdout
        assert "Created planning task" in result.stdout
        # DB was created.
        assert (repo / ".pollypm" / "state.db").exists()

    def test_new_declines_does_not_create_task(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """User declines — the project is registered but no task is created."""
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner", lambda default_yes=True: False,
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Registered project" in result.stdout
        assert "Planner skipped" in result.stdout
        assert "Created planning task" not in result.stdout
        # No work-service DB means no task was created.
        assert not (repo / ".pollypm" / "state.db").exists()

    def test_new_skip_planner_flag_bypasses_prompt(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        # Prompt should NOT be called — sentinel catches accidental invocations.
        def boom(default_yes=True):
            raise AssertionError("prompt should not be called with --skip-planner")

        monkeypatch.setattr(project_cli, "_prompt_run_planner", boom)

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--skip-planner", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Registered project" in result.stdout
        assert "Skipped planner" in result.stdout
        assert "Created planning task" not in result.stdout

    def test_new_yes_flag_auto_accepts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        def boom(default_yes=True):
            raise AssertionError("prompt should not fire with --yes")

        monkeypatch.setattr(project_cli, "_prompt_run_planner", boom)

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--yes", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Created planning task" in result.stdout


# ---------------------------------------------------------------------------
# Profile provider-policy declarations
# ---------------------------------------------------------------------------


class TestProviderPolicy:
    """Verify the frontmatter declarations requested in pp10.

    The core_agent_profiles plugin is Python-generated (no markdown loader
    on its persona path), so these files exist specifically to declare
    the provider policy in a format ``pm plugins show`` can surface.
    """

    PROFILES_DIR = (
        Path(__file__).resolve().parents[1]
        / "src" / "pollypm" / "plugins_builtin"
        / "core_agent_profiles" / "profiles"
    )

    def test_russell_profile_declares_claude_first(self) -> None:
        text = (self.PROFILES_DIR / "russell.md").read_text()
        assert text.startswith("---\n")
        # claude must appear before codex in the list.
        head = text.split("---", 2)[1]
        assert "preferred_providers" in head
        claude_idx = head.find("claude")
        codex_idx = head.find("codex")
        assert claude_idx >= 0 and codex_idx >= 0
        assert claude_idx < codex_idx

    def test_worker_profile_declares_codex_first(self) -> None:
        text = (self.PROFILES_DIR / "worker.md").read_text()
        assert text.startswith("---\n")
        head = text.split("---", 2)[1]
        assert "preferred_providers" in head
        claude_idx = head.find("claude")
        codex_idx = head.find("codex")
        assert claude_idx >= 0 and codex_idx >= 0
        assert codex_idx < claude_idx

    def test_conventions_doc_describes_policy(self) -> None:
        conventions = (
            Path(__file__).resolve().parents[1] / "docs" / "conventions.md"
        ).read_text()
        assert "Provider policy" in conventions
        assert "preferred_providers" in conventions
        # Override precedence table.
        assert "Override" in conventions or "override" in conventions


# ---------------------------------------------------------------------------
# Plugin wiring — initialize + observer
# ---------------------------------------------------------------------------


class TestPluginWiring:
    def test_plugin_declares_project_created_observer(self) -> None:
        from pollypm.plugins_builtin.project_planning import plugin as p
        assert "project.created" in p.plugin.observers
        assert len(p.plugin.observers["project.created"]) >= 1

    def test_plugin_initialize_records_event(self, tmp_path: Path) -> None:
        """initialize(api) should emit an observability event."""
        from pollypm.plugin_api.v1 import PluginAPI
        from pollypm.plugins_builtin.project_planning import plugin as p

        events: list[tuple[str, dict]] = []

        class Store:
            def record_event(self, *, kind: str, payload: dict) -> None:
                events.append((kind, payload))

        api = PluginAPI(
            plugin_name="project_planning",
            roster_api=None,
            jobs_api=None,
            host=None,
            config=None,
            state_store=Store(),
        )
        p.plugin.initialize(api)  # type: ignore[arg-type]
        assert any("initialize" in kind for kind, _ in events)
        # Payload carries the profile list and hook-registration manifest.
        _, payload = events[0]
        assert "profiles" in payload
        assert "observers" in payload
