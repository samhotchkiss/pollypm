"""Interactive UI CLI commands.

Contract:
- Inputs: Typer options selecting which TUI surface to launch.
- Outputs: root command registrations on the passed Typer app.
- Side effects: starts Textual / tmux-backed interactive screens.
- Invariants: cockpit launch plumbing is isolated from unrelated CLI
  concerns.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH


def _enforce_migration_gate(config_path: Path) -> None:
    """Refuse-start guard (#717). Best-effort — a missing config is not
    a schema-migration problem and onboarding handles it elsewhere."""
    from pollypm.store import migrations as _migrations

    if _migrations.bypass_env_is_set():
        return
    try:
        from pollypm.config import load_config
        config = load_config(config_path)
    except Exception:  # noqa: BLE001
        return
    _migrations.require_no_pending_or_exit(config.project.state_db)


def register_ui_commands(app: typer.Typer) -> None:
    @app.command()
    def accounts_ui(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        from pollypm.account_tui import AccountsApp

        AccountsApp(config_path).run()

    @app.command()
    def ui(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        from pollypm.control_tui import PollyPMApp

        PollyPMApp(config_path).run()

    @app.command()
    def cockpit(
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        _enforce_migration_gate(config_path)
        crash_log = config_path.parent / "cockpit_crash.log"
        debug_log = config_path.parent / "cockpit_debug.log"
        try:
            with open(debug_log, "a") as debug_handle:
                debug_handle.write(f"\n--- START {datetime.now().isoformat()} ---\n")
            from pollypm.cockpit_ui import PollyCockpitApp

            PollyCockpitApp(config_path).run(mouse=True)
            with open(debug_log, "a") as debug_handle:
                debug_handle.write(f"--- CLEAN EXIT {datetime.now().isoformat()} ---\n")
        except Exception:
            with open(crash_log, "a") as crash_handle:
                crash_handle.write(f"\n--- {datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=crash_handle)
            with open(debug_log, "a") as debug_handle:
                debug_handle.write(f"--- CRASH {datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=debug_handle)
            raise

    @app.command("cockpit-pane")
    def cockpit_pane(
        kind: str = typer.Argument(..., help="Pane type: inbox, settings, workers, metrics, activity, or project."),
        target: str | None = typer.Argument(None, help="Optional project key for project panes."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
        project: str | None = typer.Option(
            None,
            "--project",
            "-p",
            help=(
                "Preload a project filter (currently used by `activity` to "
                "scope the feed to one project)."
            ),
        ),
        task_id: str | None = typer.Option(
            None,
            "--task",
            help="Preselect a task in task-oriented cockpit panes.",
        ),
    ) -> None:
        _enforce_migration_gate(config_path)
        if kind == "settings" and target:
            from pollypm.cockpit_ui import PollyProjectSettingsApp

            PollyProjectSettingsApp(config_path, target).run(mouse=True)
            return
        if kind == "settings":
            from pollypm.cockpit_ui import PollySettingsPaneApp

            PollySettingsPaneApp(config_path).run(mouse=True)
            return
        if kind in ("polly", "dashboard"):
            from pollypm.cockpit_ui import PollyDashboardApp

            PollyDashboardApp(config_path).run(mouse=True)
            return
        if kind == "inbox":
            from pollypm.cockpit_ui import PollyInboxApp

            # #751 — ``--project <key>`` pre-scopes the inbox to the
            # given project on mount. Used when jumping from a
            # project dashboard so the user sees only that project's
            # items on arrival.
            project_key = project or target
            PollyInboxApp(config_path, initial_project=project_key).run(mouse=True)
            return
        if kind == "workers":
            from pollypm.cockpit_ui import PollyWorkerRosterApp

            PollyWorkerRosterApp(config_path).run(mouse=True)
            return
        if kind == "metrics":
            from pollypm.cockpit_ui import PollyMetricsApp

            PollyMetricsApp(config_path).run(mouse=True)
            return
        if kind == "issues" and target:
            from pollypm.cockpit_tasks import PollyTasksApp

            PollyTasksApp(
                config_path,
                target,
                initial_task_id=task_id,
            ).run(mouse=True)
            return
        if kind == "activity":
            from pollypm.cockpit_ui import PollyActivityFeedApp

            project_key = project or target
            PollyActivityFeedApp(config_path, project_key=project_key).run(mouse=True)
            return
        if kind == "project" and target:
            from pollypm.cockpit_ui import PollyProjectDashboardApp

            PollyProjectDashboardApp(config_path, target).run(mouse=True)
            return
        from pollypm.cockpit_ui import PollyCockpitPaneApp

        PollyCockpitPaneApp(config_path, kind, target).run(mouse=True)
