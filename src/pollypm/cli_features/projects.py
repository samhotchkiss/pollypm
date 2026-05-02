"""Project-management CLI commands.

Contract:
- Inputs: Typer options/arguments for workspace and project management.
- Outputs: root command registrations on the passed Typer app.
- Side effects: project registration, observer dispatch, tracker setup,
  and history import.
- Invariants: project lifecycle commands stay isolated from the main
  CLI entry module.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH, load_config
from pollypm.projects import (
    commit_initial_scaffold,
    enable_tracked_project,
    register_project,
    scan_projects as scan_projects_registry,
)

logger = logging.getLogger(__name__)


def _emit_auto_plan_status(
    *,
    project_key: str,
    project_path: Path,
    config_path: Path,
    skip_plan: bool,
) -> None:
    """Print a positive-confirmation line about auto-bootstrap planning.

    Mirrors the suppression precedence inside
    ``project_planning._on_project_created`` so the stdout summary
    matches what the observer actually did. See issue #1031: the
    observer's return value (``list[str]`` of failure messages) does
    not say which branch ran, so we re-derive it here.

    Branches:

    * ``skip_plan`` flag → "auto-planning skipped" hint.
    * ``[planner] auto_on_project_created=false`` → globally disabled
      hint.
    * Otherwise → look up the auto-fired ``plan_project`` task in the
      project's work DB and surface its ``task_id`` + ``status``. If
      the lookup fails (DB never opened, schema error, etc.) emit a
      degraded note pointing at ``pm project plan``.
    """
    if skip_plan:
        typer.echo(
            f"Auto-planning skipped (--skip-plan). Run "
            f"`pm project plan {project_key}` when ready."
        )
        return

    auto_fire = True
    workspace_db_path: Path | None = None
    try:
        cfg = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "auto-plan status: config read failed (%s); assuming auto_fire=True",
            exc,
        )
        cfg = None
    if cfg is not None:
        try:
            auto_fire = bool(cfg.planner.auto_on_project_created)
        except Exception:  # noqa: BLE001
            auto_fire = True
        workspace_root = getattr(getattr(cfg, "project", None), "workspace_root", None)
        if workspace_root is not None:
            workspace_db_path = Path(workspace_root) / ".pollypm" / "state.db"

    if not auto_fire:
        typer.echo(
            "Auto-planning is disabled globally "
            "([planner] auto_on_project_created=false in pollypm.toml).\n"
            f"Run `pm project plan {project_key}` to plan this project manually."
        )
        return

    db_path = workspace_db_path or (project_path / ".pollypm" / "state.db")
    task_id: str | None = None
    status: str | None = None
    if db_path.exists():
        try:
            from pollypm.work.sqlite_service import SQLiteWorkService

            with SQLiteWorkService(
                db_path=db_path, project_path=project_path,
            ) as svc:
                tasks = svc.list_tasks(project=project_key)
            plan_tasks = [
                t for t in tasks if getattr(t, "flow_template_id", None) == "plan_project"
            ]
            if plan_tasks:
                # Newest match wins. ``list_tasks`` returns ``created_at
                # ASC``, so the last entry is the freshest plan task.
                latest = plan_tasks[-1]
                task_id = getattr(latest, "task_id", None)
                work_status = getattr(latest, "work_status", None)
                # ``work_status`` is a ``WorkStatus`` enum; render its
                # ``.value`` so users see ``queued`` not
                # ``WorkStatus.QUEUED``.
                if work_status is not None:
                    status = getattr(work_status, "value", str(work_status))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "auto-plan status: work-DB lookup failed for %s (%s)",
                project_key, exc,
            )

    if task_id is not None:
        status_str = status or "unknown"
        typer.echo(
            f"Auto-fired plan_project task {task_id} (status={status_str}).\n"
            "The architect will engage on the next assignment sweep.\n"
            f"Watch progress: pm cockpit, or pm task list --project {project_key}."
        )
        return

    # Best-effort fallback: observer ran without raising, but we can't
    # find a plan_project task. Either the work DB never opened (the
    # auto-fire silently bailed in the observer's inner try-blocks) or
    # the task was never created. Don't claim success.
    typer.echo(
        "Auto-planning was attempted but no plan_project task could be "
        f"confirmed for {project_key}. Run `pm task list --project "
        f"{project_key}` to inspect, or `pm project plan {project_key}` "
        "to plan manually.",
        err=True,
    )


def register_project_commands(app: typer.Typer) -> None:
    @app.command(
        help=help_with_examples(
            "List the projects registered in the current PollyPM workspace.",
            [
                ("pm projects", "show every registered project"),
                (
                    "pm projects --config ~/.pollypm/pollypm.toml",
                    "inspect a specific config file",
                ),
            ],
        )
    )
    def projects(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        config = load_config(config_path)
        typer.echo(f"Workspace root: {config.project.workspace_root}")
        if not config.projects:
            typer.echo("No known projects.")
            return
        for key, project in config.projects.items():
            typer.echo(f"- {key}: {project.name or key} [{project.path}]")

    @app.command(
        help=(
            "Walk a directory tree, find git repos PollyPM doesn't yet "
            "track, and interactively register them."
        ),
    )
    def scan_projects(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        scan_root: Path = typer.Option(Path.home(), "--scan-root", help="Directory to scan for git repos."),
    ) -> None:
        added = scan_projects_registry(config_path, scan_root=scan_root, interactive=True)
        if not added:
            typer.echo("No new projects were added.")
            return
        typer.echo("Added projects:")
        for project in added:
            typer.echo(f"- {project.name or project.key}: {project.path}")

    @app.command(
        help=help_with_examples(
            "Register a repository as a PollyPM project.",
            [
                ("pm add-project ~/dev/my-app", "register a repo and import its history"),
                (
                    'pm add-project ~/dev/my-app --name "My App"',
                    "override the display name",
                ),
                (
                    "pm add-project ~/dev/my-app --skip-plan",
                    "register the repo without planner auto-fire",
                ),
            ],
        )
    )
    def add_project(
        repo_path: Path = typer.Argument(..., help="Path to the project folder."),
        name: str | None = typer.Option(None, "--name", help="Optional display name."),
        skip_import: bool = typer.Option(False, "--skip-import", help="Skip history import."),
        skip_plan: bool = typer.Option(
            False,
            "--skip-plan",
            help=(
                "Suppress the project_planning auto-fire for this project. "
                "See `[planner] auto_on_project_created` in pollypm.toml for "
                "the global switch."
            ),
        ),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        from pollypm.projects import normalize_project_path

        was_preexisting = False
        try:
            normalized_new = normalize_project_path(repo_path)
            existing = load_config(config_path)
            for known in existing.projects.values():
                if normalize_project_path(Path(known.path)) == normalized_new:
                    was_preexisting = True
                    break
        except Exception:  # noqa: BLE001
            was_preexisting = False

        project = register_project(config_path, repo_path, name=name)
        typer.echo(f"Registered project {project.name or project.key} at {project.path}")

        if not was_preexisting:
            observer_ran = False
            try:
                from pollypm.plugin_host import extension_host_for_root

                host = extension_host_for_root(str(project.path))
                host.run_observers(
                    "project.created",
                    {
                        "project_key": project.key,
                        "path": str(project.path),
                        "skip_plan": bool(skip_plan),
                    },
                    metadata={
                        "source": "pm add-project",
                        "config_path": str(config_path),
                    },
                )
                observer_ran = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "project.created observer failed for %s: %s",
                    project.key,
                    exc,
                )
                typer.echo(
                    f"Warning: project.created hook failed ({exc}). The project "
                    f"is registered, but the planner auto-fire did not run. Run "
                    f"`pm project plan {project.key}` to start planning manually.",
                    err=True,
                )

            # #1031: surface positive end-to-end confirmation that the
            # auto-bootstrap planning chain engaged. The observer return
            # value is just a list of failure strings, so we re-derive the
            # branch the planner observer took:
            #
            #   1. ``--skip-plan`` flag → user opted out for this run.
            #   2. ``[planner] auto_on_project_created=false`` → globally
            #      disabled; the user has to run ``pm project plan`` by
            #      hand.
            #   3. Otherwise the planner *should* have auto-fired a
            #      ``plan_project`` task. We confirm by querying the
            #      project's work DB for the most-recent matching task
            #      and report its task_id + status. If that lookup
            #      fails we degrade to a best-effort note rather than
            #      claiming success that didn't happen.
            if observer_ran:
                _emit_auto_plan_status(
                    project_key=project.key,
                    project_path=project.path,
                    config_path=config_path,
                    skip_plan=bool(skip_plan),
                )

        if skip_import:
            # Commit whatever scaffold files we wrote up to this point so
            # the project root is clean even on the --skip-import path
            # (#926).
            try:
                if commit_initial_scaffold(project.path):
                    typer.echo("Committed PollyPM scaffolding to the project repo.")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "commit_initial_scaffold failed for %s: %s", project.key, exc,
                )
            return
        typer.echo("Importing project history (transcripts, git, files)...")
        from pollypm.history_import import import_project_history

        try:
            result = import_project_history(
                project.path,
                project.name or project.key,
                skip_interview=True,
            )
            typer.echo(
                f"Import complete: {result.sources_found} sources, "
                f"{result.timeline_events} events, {result.docs_generated} docs generated"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("history_import failed for %s: %s", project.key, exc)
            typer.echo(
                f"Failed: history import for {project.key}: {exc}\n"
                f"The project is registered — rerun `pm import {project.key}` "
                f"to retry.",
                err=True,
            )

        # Commit the scaffolding (#926). Done after history-import so
        # the generated docs/ files land in the same commit as
        # .gitignore/issues/. Best-effort — never fail registration on
        # commit issues.
        try:
            if commit_initial_scaffold(project.path):
                typer.echo("Committed PollyPM scaffolding to the project repo.")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "commit_initial_scaffold failed for %s: %s", project.key, exc,
            )

    @app.command("import")
    def import_history(
        project_key: str = typer.Argument(..., help="Project key to import."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Run the history import pipeline for a project (crawl transcripts, git, files)."""
        config = load_config(config_path)
        project = config.projects.get(project_key)
        if project is None:
            typer.echo(f"Unknown project: {project_key}. Run `pm projects` to list.")
            raise typer.Exit(code=1)
        typer.echo(f"Importing history for {project.name or project_key} at {project.path}...")
        from pollypm.history_import import import_project_history

        result = import_project_history(
            project.path,
            project.name or project_key,
            skip_interview=True,
        )
        typer.echo(
            "Import complete:\n"
            f"  Sources discovered: {result.sources_found}\n"
            f"  Timeline events: {result.timeline_events}\n"
            f"  Docs generated: {result.docs_generated}\n"
            f"  Provider transcripts copied: {result.provider_transcripts_copied}"
        )
        if result.interview_questions:
            n_questions = len(result.interview_questions)
            question_word = "question" if n_questions == 1 else "questions"
            typer.echo(f"\nGenerated {n_questions} review {question_word}.")
            for question in result.interview_questions[:5]:
                typer.echo(f"  - {question}")

    @app.command(
        "init-tracker",
        help=(
            "Enable tracked-project mode for a registered project so "
            "PollyPM auto-routes work to it."
        ),
    )
    def init_tracker(
        project: str = typer.Argument(..., help="Project key."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        tracked = enable_tracked_project(config_path, project)
        typer.echo(f"Enabled tracked-project mode for {tracked.name or tracked.key}")
