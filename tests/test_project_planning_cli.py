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
            workspace_root=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
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
# blocker-summary  (#779)
# ---------------------------------------------------------------------------


class TestBlockerSummary:
    """``pm project blocker-summary`` records a structured blocker
    summary for a project (#779).

    The PM (or user acting as PM) authors this when a project enters
    a user-blocked state so the dashboard, inbox, and downstream
    consumers stop having to infer the blocker reason from free-form
    notify text.
    """

    def test_user_owned_summary_creates_inbox_task(self, tmp_path: Path) -> None:
        from pollypm.work.inbox_view import inbox_tasks
        from pollypm.work.sqlite_service import SQLiteWorkService

        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})
        # Initialize the project's work DB so blocker-summary has a
        # service to write to. ``record_project_blocker_summary``
        # refuses to run without one.
        db_path = tmp_path / ".pollypm" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with SQLiteWorkService(db_path=db_path, project_path=repo):
            pass

        result = runner.invoke(
            project_app,
            [
                "blocker-summary", "demo",
                "--reason", "Plan needs user approval before workers can claim.",
                "--owner", "user",
                "--action", "Read docs/project-plan.md",
                "--action", "Approve via pm task approve demo/3",
                "--affected", "demo/3",
                "--unblock-when", "Plan transitions out of user_review",
                "--config", str(config_path),
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Recorded blocker summary for demo" in result.stdout
        assert "Created user-facing unblock task: demo/" in result.stdout

        # Verify the inbox task lands.
        with SQLiteWorkService(db_path=db_path, project_path=repo) as svc:
            tasks = inbox_tasks(svc, project="demo")
            assert len(tasks) == 1
            task = svc.get(tasks[0].task_id)
            assert "project_blocker" in task.labels
            assert "Plan needs user approval" in task.description

    def test_unknown_project_exits_nonzero(self, tmp_path: Path) -> None:
        config_path = _write_minimal_config(tmp_path, projects={})
        result = runner.invoke(
            project_app,
            [
                "blocker-summary", "ghost",
                "--reason", "anything",
                "--owner", "user",
                "--config", str(config_path),
            ],
        )
        assert result.exit_code == 1
        assert "not registered" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# init-guide / list-guides
# ---------------------------------------------------------------------------


class TestGuides:
    def test_init_guide_worker_creates_project_local_copy(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["init-guide", "worker", "--project", "demo", "--config", str(config_path)],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        guide_path = repo / ".pollypm" / "project-guides" / "worker.md"
        assert guide_path.exists()
        text = guide_path.read_text()
        assert "forked_from:" in text
        assert "# Worker Guide" in text
        assert "pm task done" in text

    def test_init_guide_force_overwrites_existing_copy(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})
        guide_path = repo / ".pollypm" / "project-guides" / "worker.md"
        guide_path.parent.mkdir(parents=True, exist_ok=True)
        guide_path.write_text("custom old body\n")

        result = runner.invoke(
            project_app,
            [
                "init-guide",
                "worker",
                "--project",
                "demo",
                "--force",
                "--config",
                str(config_path),
            ],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        text = guide_path.read_text()
        assert "custom old body" not in text
        assert "forked_from:" in text
        assert "# Worker Guide" in text

    def test_list_guides_shows_role_path_and_fork_sha(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        for role in ("architect", "worker"):
            result = runner.invoke(
                project_app,
                ["init-guide", role, "--project", "demo", "--config", str(config_path)],
            )
            assert result.exit_code == 0, result.stdout + result.stderr

        result = runner.invoke(
            project_app,
            ["list-guides", "--project", "demo", "--config", str(config_path)],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Project-local guides for 'demo':" in result.stdout
        assert "architect:" in result.stdout
        assert "worker:" in result.stdout
        assert "forked_from:" in result.stdout
        assert str(repo / ".pollypm" / "project-guides" / "architect.md") in result.stdout

    def test_init_guide_rejects_operator_pm(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["init-guide", "operator_pm", "--project", "demo", "--config", str(config_path)],
        )

        assert result.exit_code == 1
        assert "operator_pm" in (result.stdout + result.stderr)
        assert "built-in operator guide" in (result.stdout + result.stderr)

    def test_init_guide_requires_role_argument(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["init-guide", "--project", "demo", "--config", str(config_path)],
        )

        assert result.exit_code != 0
        assert "Missing argument" in (result.stdout + result.stderr)

    def test_guide_diff_shows_unified_diff_when_upstream_changes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import pollypm.project_guides as project_guides

        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["init-guide", "worker", "--project", "demo", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        original_text = project_guides.built_in_guide_text
        original_ref = project_guides.built_in_guide_fork_ref
        monkeypatch.setattr(
            "pollypm.project_guides.built_in_guide_text",
            lambda role: "# Worker Guide\n\nNew upstream guidance\n"
            if role == "worker"
            else original_text(role),
        )
        monkeypatch.setattr(
            "pollypm.project_guides.built_in_guide_fork_ref",
            lambda role, **kwargs: "new-upstream-sha"
            if role == "worker"
            else original_ref(role, **kwargs),
        )

        result = runner.invoke(
            project_app,
            ["guide-diff", "worker", "--project", "demo", "--config", str(config_path)],
        )

        assert result.exit_code == 0, result.stdout + result.stderr
        assert "--- project-local/worker.md" in result.stdout
        assert "+++ built-in/worker.md" in result.stdout
        assert "+New upstream guidance" in result.stdout

    def test_guide_diff_requires_existing_local_guide(self, tmp_path: Path) -> None:
        repo = _make_project_repo(tmp_path)
        config_path = _write_minimal_config(tmp_path, projects={"demo": repo})

        result = runner.invoke(
            project_app,
            ["guide-diff", "worker", "--project", "demo", "--config", str(config_path)],
        )

        assert result.exit_code == 1
        assert "No project-local worker guide exists" in (result.stdout + result.stderr)


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
        # Work-service DB is workspace-scoped, not project-local.
        assert (tmp_path / ".pollypm" / "state.db").exists()
        assert not (repo / ".pollypm" / "state.db").exists()

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
            db_path=tmp_path / ".pollypm" / "state.db",
            project_path=tmp_path,
        ) as svc:
            task = svc.get(payload["task_id"])
            assert "Replan" in task.title


# ---------------------------------------------------------------------------
# new — register + prompt
# ---------------------------------------------------------------------------


def _disable_auto_plan_config(tmp_path: Path) -> Path:
    """Rewrite the minimal config so `[planner] auto_on_project_created`
    is False — used by tests that want to exercise the old prompt flow.
    """
    config_path = _write_minimal_config(tmp_path)
    text = config_path.read_text()
    if "[planner]" not in text:
        text = text.rstrip() + "\n\n[planner]\nauto_on_project_created = false\n"
    config_path.write_text(text)
    return config_path


class TestNew:
    def test_new_accepts_and_creates_plan_task(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Happy path — auto-fire creates a plan_project task by default."""
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        # Prompt should not be called — auto-fire satisfies the intent
        # before the prompt path runs (issue #255).
        def boom(default_yes=True):
            raise AssertionError("prompt should not fire when auto_on_project_created=True")

        monkeypatch.setattr(project_cli, "_prompt_run_planner", boom)

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Registered project" in result.stdout
        # Auto-fire branch produces this message; the explicit-prompt
        # branch produces "Created planning task …".
        assert "Auto-created plan_project task" in result.stdout
        # DB was created at workspace scope by the observer.
        assert (tmp_path / ".pollypm" / "state.db").exists()
        assert not (repo / ".pollypm" / "state.db").exists()

    def test_new_declines_does_not_create_task(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """User declines the prompt — only meaningful when auto-fire is
        disabled. With ``[planner] auto_on_project_created = false`` the
        legacy prompt still runs and declining produces no task.
        """
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _disable_auto_plan_config(tmp_path)

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
        assert "Auto-created plan_project task" not in result.stdout
        # No work-service DB means no task was created.
        assert not (tmp_path / ".pollypm" / "state.db").exists()

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
        assert "Auto-created plan_project task" not in result.stdout

    def test_new_yes_flag_auto_accepts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``--yes`` is a no-op when auto-fire is already enabled — the
        observer creates the task and the prompt path is skipped."""
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
        assert "Auto-created plan_project task" in result.stdout

    def test_new_yes_flag_with_auto_disabled_uses_prompt_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With auto-fire disabled, ``--yes`` drives the legacy explicit
        task-creation path, producing the ``Created planning task`` echo.
        """
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _disable_auto_plan_config(tmp_path)

        def boom(default_yes=True):
            raise AssertionError("prompt should not fire with --yes")

        monkeypatch.setattr(project_cli, "_prompt_run_planner", boom)

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--yes", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Created planning task" in result.stdout

    # -----------------------------------------------------------------
    # Issue #255: auto-fire the planner on project.created
    # -----------------------------------------------------------------

    def test_new_default_auto_fires_plan_project_task(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Fresh project + default config → a plan_project task exists."""
        from pollypm.work.sqlite_service import SQLiteWorkService

        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire")
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        with SQLiteWorkService(
            db_path=tmp_path / ".pollypm" / "state.db",
            project_path=tmp_path,
        ) as svc:
            tasks = svc.list_tasks(project="fresh")
        plan_tasks = [t for t in tasks if t.flow_template_id == "plan_project"]
        assert len(plan_tasks) == 1, f"expected exactly 1 plan_project task, got {tasks}"

    def test_new_skip_plan_suppresses_auto_fire(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        # Prompt should not fire either — --skip-plan implies no planner
        # UI altogether for this invocation.
        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire when --skip-plan passed")
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--skip-plan", "--yes", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # No plan_project task was created (no DB at all).
        assert not (tmp_path / ".pollypm" / "state.db").exists()
        assert "Auto-created plan_project task" not in result.stdout

    def test_new_config_disable_suppresses_auto_fire(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`[planner] auto_on_project_created = false` suppresses globally."""
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _disable_auto_plan_config(tmp_path)

        # Decline the prompt so we can assert nothing else fires.
        monkeypatch.setattr(
            project_cli, "_prompt_run_planner", lambda default_yes=True: False,
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert not (tmp_path / ".pollypm" / "state.db").exists()
        assert "Auto-created plan_project task" not in result.stdout

    # -----------------------------------------------------------------
    # Issue #993: auto-fired plan_project task must move past draft so
    # the architect's assignment sweep actually has work to push.
    # -----------------------------------------------------------------

    def test_new_auto_fired_plan_task_is_queued(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The auto-fired plan_project task must NOT linger in draft.

        Regression for #993: previously the observer created the task in
        ``draft`` and the architect spawn would sit idle forever because
        the task-assignment sweep only pushes ``queued``/``in_progress``
        tasks. After the fix the task should be at least ``queued`` (the
        sweep may transition it onward to ``in_progress`` synchronously
        if a worker session is wired, but ``queued`` is the contract).
        """
        from pollypm.work.models import WorkStatus
        from pollypm.work.sqlite_service import SQLiteWorkService

        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire")
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--yes", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr

        with SQLiteWorkService(
            db_path=tmp_path / ".pollypm" / "state.db",
            project_path=tmp_path,
        ) as svc:
            tasks = [
                t for t in svc.list_tasks(project="fresh")
                if t.flow_template_id == "plan_project"
            ]
        assert len(tasks) == 1, f"expected 1 plan_project task, got {tasks}"
        task = tasks[0]
        assert task.work_status != WorkStatus.DRAFT, (
            "auto-fired plan_project task must be moved past draft so the "
            "architect assignment sweep has something to push (#993). "
            f"Got status={task.work_status.value!r}."
        )
        # Either queued (no live architect session yet) or in_progress
        # (sweep already promoted it). Both are acceptable post-fix; the
        # bug was the task sitting in draft indefinitely.
        assert task.work_status in (
            WorkStatus.QUEUED, WorkStatus.IN_PROGRESS,
        ), (
            f"expected queued or in_progress, got {task.work_status.value!r}"
        )

    def test_new_skip_plan_creates_no_task_at_all(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``--skip-plan`` must keep the auto-fire suppressed entirely.

        The #993 fix added an auto-queue step — verify it doesn't kick in
        when the user explicitly opted out. ``--skip-plan`` should still
        leave no work-service DB and no plan_project task.
        """
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        monkeypatch.setattr(
            project_cli, "_prompt_run_planner",
            lambda default_yes=True: (_ for _ in ()).throw(
                AssertionError("prompt should not fire when --skip-plan passed")
            ),
        )

        result = runner.invoke(
            project_app,
            ["new", str(repo), "--skip-plan", "--yes",
             "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # Observer short-circuits before create() so no DB, no task, and
        # certainly no draft-in-disguise.
        assert not (tmp_path / ".pollypm" / "state.db").exists()


# ---------------------------------------------------------------------------
# Issue #255 — pm add-project auto-fire
# ---------------------------------------------------------------------------


class TestAddProjectAutoFire:
    """`pm add-project` must emit `project.created` symmetrically with
    `pm project new` and honour `--skip-plan` + the config toggle.
    """

    def _invoke_add_project(
        self,
        *,
        repo: Path,
        config_path: Path,
        extra_args: list[str] | None = None,
    ):
        """Drive ``pm add-project`` via its Typer app."""
        from pollypm.cli import app as root_app

        args = [
            "add-project", str(repo), "--skip-import",
            "--config", str(config_path),
            "--name", repo.name,
        ]
        args.extend(extra_args or [])
        return runner.invoke(root_app, args)

    def test_add_project_default_fires_plan_project(
        self, tmp_path: Path
    ) -> None:
        """Default config → ``pm add-project`` auto-creates a plan task.

        This is the direct fix for #255 — previously `pm add-project`
        did not emit `project.created` at all, so the planner never
        ran on projects added that way.
        """
        from pollypm.work.sqlite_service import SQLiteWorkService

        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        result = self._invoke_add_project(repo=repo, config_path=config_path)
        assert result.exit_code == 0, result.stdout + result.stderr

        db_path = tmp_path / ".pollypm" / "state.db"
        assert db_path.exists(), "auto-fire should have opened the work DB"
        with SQLiteWorkService(db_path=db_path, project_path=tmp_path) as svc:
            tasks = svc.list_tasks(project="fresh")
        plan_tasks = [t for t in tasks if t.flow_template_id == "plan_project"]
        assert len(plan_tasks) == 1

    def test_add_project_skip_plan_suppresses_auto_fire(
        self, tmp_path: Path
    ) -> None:
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        result = self._invoke_add_project(
            repo=repo, config_path=config_path,
            extra_args=["--skip-plan"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        # --skip-plan means no observer-driven task, so no DB either.
        assert not (tmp_path / ".pollypm" / "state.db").exists()

    def test_add_project_config_disabled_suppresses_auto_fire(
        self, tmp_path: Path
    ) -> None:
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _disable_auto_plan_config(tmp_path)

        result = self._invoke_add_project(repo=repo, config_path=config_path)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert not (tmp_path / ".pollypm" / "state.db").exists()

    def test_add_project_re_add_does_not_double_fire(
        self, tmp_path: Path
    ) -> None:
        """Second ``pm add-project`` on the same path must not re-fire
        `project.created` — that would auto-create a second plan task.
        """
        from pollypm.work.sqlite_service import SQLiteWorkService

        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        # First add — observer fires, task created.
        result = self._invoke_add_project(repo=repo, config_path=config_path)
        assert result.exit_code == 0, result.stdout + result.stderr
        with SQLiteWorkService(
            db_path=tmp_path / ".pollypm" / "state.db",
            project_path=tmp_path,
        ) as svc:
            first_tasks = svc.list_tasks(project="fresh")
        assert len([t for t in first_tasks if t.flow_template_id == "plan_project"]) == 1

        # Second add — observer must not fire.
        result = self._invoke_add_project(repo=repo, config_path=config_path)
        assert result.exit_code == 0, result.stdout + result.stderr
        with SQLiteWorkService(
            db_path=tmp_path / ".pollypm" / "state.db",
            project_path=tmp_path,
        ) as svc:
            second_tasks = svc.list_tasks(project="fresh")
        plan_tasks = [t for t in second_tasks if t.flow_template_id == "plan_project"]
        assert len(plan_tasks) == 1, (
            f"re-add should not double-fire; got {len(plan_tasks)} plan tasks"
        )

    # -----------------------------------------------------------------
    # Issue #1031 — pm add-project must surface the planner outcome to
    # stdout so users have a positive end-to-end confirmation that
    # auto-bootstrap planning engaged.
    # -----------------------------------------------------------------

    def test_add_project_default_emits_auto_fire_status_to_stdout(
        self, tmp_path: Path
    ) -> None:
        """Default config success branch: stdout names the auto-fired
        plan_project task and its queued status (#1031).
        """
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        result = self._invoke_add_project(repo=repo, config_path=config_path)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Auto-fired plan_project task" in result.stdout, result.stdout
        # Task IDs are rendered as ``<project>/<n>``.
        assert "fresh/" in result.stdout, result.stdout
        # The queued status should round-trip through to the user (the
        # observer flips draft->queued at the end).
        assert "status=queued" in result.stdout, result.stdout
        assert "pm cockpit" in result.stdout, result.stdout
        assert "pm task list --project fresh" in result.stdout, result.stdout

    def test_add_project_skip_plan_emits_skip_hint(
        self, tmp_path: Path
    ) -> None:
        """``--skip-plan`` branch surfaces a one-line opt-in hint
        instead of claiming auto-fire happened (#1031).
        """
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _write_minimal_config(tmp_path)

        result = self._invoke_add_project(
            repo=repo, config_path=config_path,
            extra_args=["--skip-plan"],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Auto-planning skipped" in result.stdout, result.stdout
        assert "--skip-plan" in result.stdout, result.stdout
        assert "pm project plan fresh" in result.stdout, result.stdout
        # And we must NOT have lied about an auto-fire.
        assert "Auto-fired plan_project task" not in result.stdout, result.stdout

    def test_add_project_config_disabled_emits_global_hint(
        self, tmp_path: Path
    ) -> None:
        """``[planner] auto_on_project_created=false`` branch: stdout
        explains the global suppression and points at the manual verb
        (#1031).
        """
        repo = _make_project_repo(tmp_path, name="fresh")
        config_path = _disable_auto_plan_config(tmp_path)

        result = self._invoke_add_project(repo=repo, config_path=config_path)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert "Auto-planning is disabled globally" in result.stdout, result.stdout
        assert "auto_on_project_created=false" in result.stdout, result.stdout
        assert "pm project plan fresh" in result.stdout, result.stdout
        assert "Auto-fired plan_project task" not in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Issue #255 — PlannerSettings config roundtrip
# ---------------------------------------------------------------------------


class TestPlannerSettings:
    def test_default_planner_auto_on_project_created_is_true(
        self, tmp_path: Path
    ) -> None:
        from pollypm.config import load_config

        config_path = _write_minimal_config(tmp_path)
        cfg = load_config(config_path)
        assert cfg.planner.auto_on_project_created is True

    def test_planner_auto_on_project_created_round_trips_false(
        self, tmp_path: Path
    ) -> None:
        from pollypm.config import load_config

        config_path = _write_minimal_config(tmp_path)
        text = config_path.read_text()
        config_path.write_text(
            text.rstrip() + "\n\n[planner]\nauto_on_project_created = false\n"
        )
        cfg = load_config(config_path)
        assert cfg.planner.auto_on_project_created is False


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
