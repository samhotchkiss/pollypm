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

from pollypm.config import DEFAULT_CONFIG_PATH, load_config
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    scan_projects as scan_projects_registry,
)

logger = logging.getLogger(__name__)


def register_project_commands(app: typer.Typer) -> None:
    @app.command()
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

    @app.command()
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

    @app.command()
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

        if skip_import:
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
            typer.echo(f"\nGenerated {len(result.interview_questions)} review question(s).")
            for question in result.interview_questions[:5]:
                typer.echo(f"  - {question}")

    @app.command("init-tracker")
    def init_tracker(
        project: str = typer.Argument(..., help="Project key."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        tracked = enable_tracked_project(config_path, project)
        typer.echo(f"Enabled tracked-project mode for {tracked.name or tracked.key}")

