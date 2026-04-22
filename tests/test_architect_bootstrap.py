"""Tests for the architect session bootstrap (issue #257).

Covers the three surfaces the fix adds:

1. ``pm worker-start --role architect --profile architect <project>``
   spawns an ``architect_<project>`` session (CLI wiring).
2. ``role_candidate_names("architect", "<project>")`` expands to the
   architect session names so the task-assignment sweeper can resolve
   the recipient (work-service role-resolver wiring).
3. ``pm project new`` on a fresh repo auto-creates the plan_project
   task AND auto-spawns an architect session. ``--skip-planner``
   suppresses both.
4. The architect session's initial input resolves to the architect's
   control prompt (not the worker prompt), so the session starts as
   Archie even though the role is project-scoped and therefore not in
   ``_CONTROL_ROLES``.
"""
from __future__ import annotations

from pathlib import Path

import pollypm.cli as cli
from typer.testing import CliRunner

from pollypm.config import write_config
from pollypm.models import (
    AccountConfig,
    KnownProject,
    PollyPMConfig,
    PollyPMSettings,
    ProjectKind,
    ProjectSettings,
    ProviderKind,
    SessionConfig,
)
from pollypm.plugins_builtin.project_planning.cli import project as project_cli
from pollypm.plugins_builtin.project_planning.cli.project import project_app
from pollypm.work.task_assignment import role_candidate_names


runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_project_repo(tmp_path: Path, name: str = "demo") -> Path:
    """Create a minimal git-looking directory on disk."""
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
            # Pin workspace_root to tmp_path so planner-created tasks
            # land under the test's sandbox rather than the ambient
            # ``~/dev`` default picked up by ``_planner_db_path``.
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
# (1) pm worker-start --role architect
# ---------------------------------------------------------------------------


def test_worker_start_role_architect_spawns_architect_session(
    monkeypatch, tmp_path: Path,
) -> None:
    """Passing ``--role architect`` forwards the role + profile into
    ``create_worker_session`` and produces an ``architect_<project>``
    session name.
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")
    created: list[dict[str, object]] = []
    launched: list[tuple[Path, str]] = []

    class FakeSupervisor:
        def __init__(self) -> None:
            self.config = type(
                "Config",
                (),
                {
                    "sessions": {},
                    "project": type("Project", (), {"tmux_session": "pollypm"})(),
                },
            )()

        def tmux_session_for_launch(self, launch) -> str:
            return "pollypm-storage-closet"

        def plan_launches(self):
            session = type("Session", (), {"name": "architect_foo"})()
            return [
                type(
                    "Launch",
                    (),
                    {"session": session, "window_name": "architect-foo"},
                )()
            ]

    def fake_create(
        path, project_key, prompt=None, role="worker", agent_profile=None,
    ):
        created.append(
            {
                "path": path,
                "project": project_key,
                "prompt": prompt,
                "role": role,
                "agent_profile": agent_profile,
            }
        )
        return type("Session", (), {"name": "architect_foo"})()

    monkeypatch.setattr(cli, "_require_pollypm_session", lambda supervisor: None)
    monkeypatch.setattr(cli, "_load_supervisor", lambda path: FakeSupervisor())
    monkeypatch.setattr(cli, "create_worker_session", fake_create)
    monkeypatch.setattr(
        cli,
        "launch_worker_session",
        lambda path, session_name: launched.append((path, session_name)),
    )

    result = runner.invoke(
        cli.app,
        [
            "worker-start",
            "foo",
            "--role", "architect",
            "--profile", "architect",
            "--config", str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(created) == 1
    assert created[0]["project"] == "foo"
    assert created[0]["role"] == "architect"
    assert created[0]["agent_profile"] == "architect"
    assert launched == [(config_path, "architect_foo")]
    # Text banner reflects the non-worker label.
    assert "Managed architect architect_foo ready for project foo" in result.output


def test_worker_start_no_role_exits_deprecated(
    monkeypatch, tmp_path: Path,
) -> None:
    """``pm worker-start <project>`` without ``--role`` is deprecated.

    Per-task workers (provisioned automatically by ``pm task claim``)
    replaced the managed-worker pattern — a managed ``worker-<project>``
    session leaks memory because it outlives the task that needed it
    and has no cleanup hook. The CLI now exits with code 2 and a
    fix-it pointer at ``pm task claim``. ``--role architect`` is still
    supported (see the test above).
    """
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[project]\nname = \"pollypm\"\n")

    result = runner.invoke(
        cli.app, ["worker-start", "bar", "--config", str(config_path)],
    )
    assert result.exit_code == 2, result.output
    assert "deprecated" in result.output
    assert "pm task claim" in result.output
    assert "--role architect" in result.output


# ---------------------------------------------------------------------------
# (2) Role-candidate-names expansion
# ---------------------------------------------------------------------------


def test_role_candidate_names_expands_architect_to_project_scoped_session() -> None:
    """``role:architect`` expands to both ``architect-<project>`` and
    ``architect_<project>`` — matching the dual-convention worker path so
    the sweeper finds a session regardless of naming drift."""
    candidates = role_candidate_names("architect", "foo")
    assert candidates == ["architect-foo", "architect_foo"]


def test_role_candidate_names_still_expands_worker() -> None:
    """Regression: worker still expands to the dual-convention pair so
    existing task-assignment wiring continues to work."""
    candidates = role_candidate_names("worker", "foo")
    assert candidates == ["worker-foo", "worker_foo"]


def test_role_candidate_names_handles_mixed_case_architect() -> None:
    """Resolver is case-insensitive on the role key."""
    assert role_candidate_names("Architect", "demo") == [
        "architect-demo", "architect_demo",
    ]


# ---------------------------------------------------------------------------
# (3) pm project new auto-spawns an architect session
# ---------------------------------------------------------------------------


def test_project_new_auto_spawns_architect_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Fresh ``pm project new`` creates a plan_project task (existing
    behaviour) AND auto-spawns an architect session via the new
    ``_auto_spawn_architect`` helper."""
    from pollypm.work.sqlite_service import SQLiteWorkService

    repo = _make_project_repo(tmp_path, name="fresh")
    config_path = _write_minimal_config(tmp_path)

    spawned: list[tuple[Path, str]] = []

    def fake_spawn(cfg_path: Path, project_key: str) -> None:
        spawned.append((cfg_path, project_key))

    monkeypatch.setattr(project_cli, "_auto_spawn_architect", fake_spawn)

    result = runner.invoke(
        project_app, ["new", str(repo), "--config", str(config_path)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Plan task was auto-created (regression guard against #255 breakage).
    # Post-#339 planner tasks land in the workspace-scope DB
    # (``workspace_root/.pollypm/state.db``), not the project-local path.
    with SQLiteWorkService(
        db_path=tmp_path / ".pollypm" / "state.db", project_path=repo,
    ) as svc:
        tasks = svc.list_tasks(project="fresh")
    plan_tasks = [t for t in tasks if t.flow_template_id == "plan_project"]
    assert len(plan_tasks) == 1
    # ...and the architect auto-spawn helper was invoked exactly once.
    assert spawned == [(config_path, "fresh")]


def test_project_new_skip_planner_does_not_spawn_architect(
    tmp_path: Path, monkeypatch,
) -> None:
    """``--skip-planner`` suppresses both the plan task and the
    architect auto-spawn."""
    repo = _make_project_repo(tmp_path, name="fresh")
    config_path = _write_minimal_config(tmp_path)

    spawned: list[tuple[Path, str]] = []

    def fake_spawn(cfg_path: Path, project_key: str) -> None:
        spawned.append((cfg_path, project_key))

    monkeypatch.setattr(project_cli, "_auto_spawn_architect", fake_spawn)

    result = runner.invoke(
        project_app,
        ["new", str(repo), "--skip-planner", "--config", str(config_path)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # No plan DB created (no planner task).
    assert not (repo / ".pollypm" / "state.db").exists()
    # No architect spawn either — ``--skip-planner`` is a full suppressor.
    assert spawned == []


# ---------------------------------------------------------------------------
# (4) Architect session's initial input resolves to architect's control prompt
# ---------------------------------------------------------------------------


def _supervisor_with_architect(tmp_path: Path):
    """Return a Supervisor wired with a single architect session."""
    config = PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_1"),
        accounts={
            "claude_1": AccountConfig(
                name="claude_1",
                provider=ProviderKind.CLAUDE,
                email="c@example.com",
                home=tmp_path / ".pollypm/homes/claude_1",
            ),
        },
        sessions={
            "architect_demo": SessionConfig(
                name="architect_demo",
                role="architect",
                provider=ProviderKind.CLAUDE,
                account="claude_1",
                cwd=tmp_path,
                project="demo",
                window_name="architect-demo",
                agent_profile="architect",
            ),
        },
        projects={
            "demo": KnownProject(
                key="demo",
                path=tmp_path,
                name="demo",
                kind=ProjectKind.FOLDER,
            )
        },
    )
    # Imported lazily so the tmux/session_services wiring doesn't break
    # simpler tests that only touch CLI.
    from pollypm.supervisor import Supervisor

    return Supervisor(config)


def test_architect_session_resolves_architect_control_prompt(
    tmp_path: Path,
) -> None:
    """``effective_session`` on an ``architect`` role session pulls the
    architect persona's markdown prompt, NOT the worker prompt.

    This is the load-bearing check for issue #257: without this the
    architect session would start with either no prompt or (worse) the
    worker's persona, producing nonsense behaviour at research-stage
    kickoff.
    """
    sup = _supervisor_with_architect(tmp_path)
    sup.ensure_layout()

    session = sup.config.sessions["architect_demo"]
    effective = sup.effective_session(session)
    prompt = effective.prompt or ""
    assert prompt, "architect session should receive a non-empty prompt"
    # The architect persona markdown is unmistakable: it opens with
    # "You are the PollyPM Architect" inside the <identity> block, and
    # mentions "critic panel" in the principles. Assert both markers to
    # prove we're not accidentally getting the worker prompt.
    assert "PollyPM Architect" in prompt
    assert "critic panel" in prompt.lower()
    # Worker prompt opens with a different persona; guard against a
    # silent swap by asserting the worker-specific phrasing is absent.
    assert "You are a PollyPM worker" not in prompt


def test_architect_role_is_in_initial_input_roles(tmp_path: Path) -> None:
    """``_INITIAL_INPUT_ROLES`` gates whether the Supervisor actually
    sends the control prompt into the pane on fresh launch. ``architect``
    must be in the set or the session starts blank.
    """
    from pollypm.supervisor import Supervisor

    assert "architect" in Supervisor._INITIAL_INPUT_ROLES
    # But architect is NOT a control-plane role (project-scoped, not
    # control-plane). The task description pins this as a constraint.
    assert "architect" not in Supervisor._CONTROL_ROLES
