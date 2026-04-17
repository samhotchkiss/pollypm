"""``pm project`` CLI — planner-backed project lifecycle entry points.

Spec: ``docs/planner-plugin-spec.md`` §9–§10.

Subcommands:

* ``pm project new <path>`` — register a project (delegates to the core
  ``pollypm.projects.register_project``) and then prompt "Run the
  planner now? (Y/n)". Declining exits cleanly; accepting triggers the
  same path as ``pm project plan``.
* ``pm project plan [project]`` — create a task with ``flow=plan_project``.
* ``pm project replan [project]`` — create a task with ``flow=plan_project``;
  the architect's stage-0 research loop reads the existing plan and
  runs drift analysis (see ``replan.py``).

Implementation detail: the ``pm project new`` flow delegates to the
same ``_plan_project_task`` helper that the ``plan`` subcommand uses,
so both exercises the same code path end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer

from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_config_path,
)


project_app = typer.Typer(
    help="Planner-backed project lifecycle (new / plan / replan).",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _require_config(config_path: Path) -> Path:
    """Resolve + verify the pollypm config exists. Exit(1) otherwise."""
    path = resolve_config_path(config_path)
    if not path.exists():
        typer.echo(
            f"No PollyPM config at {path}. Run `pm init` or `pm onboard` first.",
            err=True,
        )
        raise typer.Exit(code=1)
    return path


def _resolve_project_key(
    config_path: Path,
    project: str | None,
) -> tuple[str, Path]:
    """Resolve a project key + path from a user-supplied identifier.

    Accepts an explicit project key, a normalized (hyphens-to-underscores)
    key, or a filesystem path. When ``project`` is ``None``, prefer the
    cwd if it matches a registered project. Exits cleanly with a helpful
    message when no match is found.
    """
    config = load_config(config_path)

    # No explicit project — try cwd.
    if project is None:
        cwd = Path.cwd().resolve()
        for key, known in config.projects.items():
            if Path(known.path).resolve() == cwd:
                return key, Path(known.path)
        typer.echo(
            "No project specified and the current directory is not a "
            "registered project. Provide the project key explicitly, or run "
            "from inside a project's root.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Explicit key or alias.
    if project in config.projects:
        return project, Path(config.projects[project].path)

    normalized = project.replace("-", "_")
    if normalized in config.projects:
        return normalized, Path(config.projects[normalized].path)

    # Filesystem path fallback.
    as_path = Path(project).expanduser().resolve()
    for key, known in config.projects.items():
        if Path(known.path).resolve() == as_path:
            return key, Path(known.path)

    typer.echo(
        f"Unknown project '{project}'. Known: "
        + (", ".join(config.projects) or "(none)"),
        err=True,
    )
    raise typer.Exit(code=1)


def _plan_project_task(
    project_key: str,
    project_path: Path,
    *,
    title_prefix: str = "Plan",
    description: str = "",
    actor: str = "architect",
) -> Any:
    """Create a ``flow=plan_project`` task on the project's work service.

    Returns the created ``Task``. Caller owns output formatting.
    """
    # Local import to keep the CLI importable in tests that haven't wired
    # a full SQLite environment yet.
    from pollypm.work.sqlite_service import SQLiteWorkService

    db_path = project_path / ".pollypm" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with SQLiteWorkService(db_path=db_path, project_path=project_path) as svc:
        task = svc.create(
            title=f"{title_prefix} {project_key}",
            description=description or (
                f"Run the architect + 5-critic planning pipeline on "
                f"{project_key}."
            ),
            type="task",
            project=project_key,
            flow_template="plan_project",
            roles={"architect": actor},
            priority="high",
        )
    return task


# ---------------------------------------------------------------------------
# pm project plan
# ---------------------------------------------------------------------------


@project_app.command("plan")
def plan_cmd(
    project: Optional[str] = typer.Argument(
        None,
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable output.",
    ),
) -> None:
    """Run the architecture planner on ``project``.

    Creates a task with ``flow=plan_project`` on the project's work
    service. The flow engine drives the architect through research,
    decomposition, critic panel, and synthesis (§3 of the planner spec).
    """
    path = _require_config(config_path)
    key, project_path = _resolve_project_key(path, project)
    task = _plan_project_task(key, project_path, title_prefix="Plan project")

    if as_json:
        typer.echo(json.dumps({
            "project": key,
            "task_id": task.task_id,
            "flow": task.flow_template_id,
            "work_status": task.work_status.value,
        }, indent=2))
        return
    typer.echo(
        f"Created planning task {task.task_id} on project '{key}' "
        f"(flow={task.flow_template_id})."
    )
    typer.echo(
        "Next: `pm task queue " + task.task_id + "` to hand it off to the "
        "architect worker."
    )


# ---------------------------------------------------------------------------
# pm project replan
# ---------------------------------------------------------------------------


@project_app.command("replan")
def replan_cmd(
    project: Optional[str] = typer.Argument(
        None,
        help=(
            "Project key, alias, or path. Defaults to the project whose "
            "root matches the current directory."
        ),
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable output.",
    ),
) -> None:
    """Re-run the planner on ``project`` with drift analysis.

    Uses the same ``plan_project`` flow; the architect's stage-0 research
    loop reads the existing ``docs/project-plan.md`` + Risk Ledger +
    ``docs/planning-session-log.md`` and produces a drift analysis
    (``replan.py``) before re-opening decomposition.
    """
    path = _require_config(config_path)
    key, project_path = _resolve_project_key(path, project)
    task = _plan_project_task(
        key, project_path, title_prefix="Replan project",
        description=(
            f"Re-run the architecture planner on {key}. Stage-0 research "
            "should read the existing plan and produce a drift analysis "
            "before proposing changes."
        ),
    )

    if as_json:
        typer.echo(json.dumps({
            "project": key,
            "task_id": task.task_id,
            "flow": task.flow_template_id,
            "mode": "replan",
            "work_status": task.work_status.value,
        }, indent=2))
        return
    typer.echo(
        f"Created replan task {task.task_id} on project '{key}' "
        f"(flow={task.flow_template_id}, mode=replan)."
    )
    typer.echo(
        "Next: `pm task queue " + task.task_id + "` to hand it off to the "
        "architect worker."
    )


# ---------------------------------------------------------------------------
# pm project new
# ---------------------------------------------------------------------------


def _prompt_run_planner(*, default_yes: bool = True) -> bool:
    """Prompt 'Run the planner now? (Y/n)'. Default yes.

    Isolated so tests can monkey-patch ``typer.confirm``.
    """
    return typer.confirm("Run the planner now?", default=default_yes)


@project_app.command("new")
def new_cmd(
    repo_path: Path = typer.Argument(
        ..., help="Path to the project folder (must be a git repo).",
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Optional display name.",
    ),
    skip_planner: bool = typer.Option(
        False, "--skip-planner",
        help="Register the project without prompting for the planner.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Non-interactive: auto-accept the planner prompt.",
    ),
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path.",
    ),
) -> None:
    """Register a project and optionally kick off the planner.

    After registration, prompts "Run the planner now? (Y/n)" (default
    yes). ``--skip-planner`` registers the project without prompting;
    ``--yes`` accepts the prompt non-interactively.
    """
    from pollypm.projects import register_project

    path = _require_config(config_path)
    project = register_project(path, repo_path, name=name)
    typer.echo(
        f"Registered project {project.name or project.key} at {project.path}"
    )

    # Fire the ``project.created`` observer chain so plugins (including
    # project_planning itself) can react. Best-effort — never block the
    # CLI on observer failures.
    try:
        from pollypm.plugin_host import extension_host_for_root

        host = extension_host_for_root(str(project.path))
        host.run_observers(
            "project.created",
            {"project_key": project.key, "path": str(project.path)},
            metadata={"source": "pm project new"},
        )
    except Exception:  # noqa: BLE001
        pass

    if skip_planner:
        typer.echo(
            "Skipped planner. Run `pm project plan "
            + project.key + "` later to start planning."
        )
        return

    if yes:
        run_it = True
    else:
        run_it = _prompt_run_planner()

    if not run_it:
        typer.echo(
            "Planner skipped. Run `pm project plan "
            + project.key + "` whenever you're ready."
        )
        return

    task = _plan_project_task(
        project.key, project.path, title_prefix="Plan project",
    )
    typer.echo(
        f"Created planning task {task.task_id} on project "
        f"'{project.key}' (flow={task.flow_template_id})."
    )
    typer.echo(
        "Next: `pm task queue " + task.task_id + "` to hand it off to the "
        "architect worker."
    )
