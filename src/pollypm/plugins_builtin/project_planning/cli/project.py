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
    help=(
        "Planner-backed project lifecycle (new / plan / replan).\n\n"
        "Examples:\n\n"
        "• pm project new <name>              — create a new planner-backed project\n"
        "• pm project plan <name>             — (re)run the tree-of-plans planner\n"
        "• pm project replan <name>           — regenerate the plan from current state\n"
    ),
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
    skip_plan: bool = typer.Option(
        False, "--skip-plan",
        help=(
            "Suppress the project_planning auto-fire for this project "
            "(issue #255). Equivalent to setting "
            "`[planner] auto_on_project_created = false` globally, but "
            "scoped to this invocation. Independent of the interactive "
            "`--skip-planner` prompt toggle."
        ),
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
    ``--yes`` accepts the prompt non-interactively. ``--skip-plan``
    suppresses the project_planning plugin's auto-fire on the emitted
    ``project.created`` event (#255).
    """
    from pollypm.projects import register_project, normalize_project_path

    path = _require_config(config_path)

    # Determine whether the path is already registered before we call
    # ``register_project`` — the function silently returns the existing
    # entry on a re-register, and we must not re-fire ``project.created``
    # for a project that already exists (issue #255 acceptance: "second
    # add-project with same name doesn't double-fire").
    was_preexisting = False
    try:
        from pollypm.config import load_config as _load_config
        normalized_new = normalize_project_path(repo_path)
        existing = _load_config(path)
        for known in existing.projects.values():
            if normalize_project_path(Path(known.path)) == normalized_new:
                was_preexisting = True
                break
    except Exception:  # noqa: BLE001
        was_preexisting = False

    project = register_project(path, repo_path, name=name)
    typer.echo(
        f"Registered project {project.name or project.key} at {project.path}"
    )

    # Fire the ``project.created`` observer chain so plugins (including
    # project_planning itself) can react. Best-effort — never block the
    # CLI on observer failures. Only fire on genuinely-new registrations
    # so a re-run of ``pm project new`` on the same path is idempotent.
    auto_fired = False
    if not was_preexisting:
        try:
            from pollypm.plugin_host import extension_host_for_root

            host = extension_host_for_root(str(project.path))
            host.run_observers(
                "project.created",
                {
                    "project_key": project.key,
                    "path": str(project.path),
                    # ``skip_plan`` travels through the event payload so
                    # the observer can suppress auto-fire without the
                    # CLI having to reach into plugin internals.
                    # ``--skip-planner`` (legacy flag that suppresses the
                    # interactive prompt + the explicit ``_plan_project_task``
                    # call below) also suppresses auto-fire so the combined
                    # UX stays "no planner task".
                    "skip_plan": bool(skip_plan or skip_planner),
                },
                metadata={
                    "source": "pm project new",
                    # Forward the effective config path so the observer
                    # can honour `[planner] auto_on_project_created`
                    # from a non-default config (notably in tests).
                    "config_path": str(path),
                },
            )
            # Detect whether the observer actually created a plan_project
            # task on the project's work service. If it did, the
            # interactive prompt below becomes redundant — auto-fire
            # already satisfied the user's "plan this project" intent.
            auto_fired = _plan_task_exists(project.path, project.key)
        except Exception:  # noqa: BLE001
            pass

    if auto_fired:
        typer.echo(
            f"Auto-created plan_project task for '{project.key}' via "
            "project.created hook (see `[planner] auto_on_project_created`)."
        )
        return

    # ``--skip-plan`` or ``--skip-planner`` both short-circuit the prompt +
    # legacy task-creation path. This keeps the CLI idempotent with the
    # event payload we sent to observers above.
    if skip_plan or skip_planner:
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


def _plan_task_exists(project_path: Path, project_key: str) -> bool:
    """Return True if a ``plan_project`` task already exists on the
    project's work service.

    Used by ``new_cmd`` after emitting ``project.created`` to detect
    whether the observer auto-fired a planning task — in which case the
    interactive prompt becomes redundant. Safe to call on a project
    that never opened its work DB (returns False without creating one).
    """
    db_path = project_path / ".pollypm" / "state.db"
    if not db_path.exists():
        return False
    try:
        from pollypm.work.sqlite_service import SQLiteWorkService

        with SQLiteWorkService(
            db_path=db_path, project_path=project_path,
        ) as svc:
            for task in svc.list_tasks(project=project_key):
                if task.flow_template_id == "plan_project":
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False
