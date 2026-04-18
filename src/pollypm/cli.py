from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

from pollypm.accounts import (
    add_account_via_login,
    list_account_statuses,
    probe_account_usage,
    relogin_account,
    remove_account as remove_account_entry,
)
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    GLOBAL_CONFIG_DIR,
    load_config,
    resolve_config_path,
    render_example_config,
    write_example_config,
)
from pollypm.doc_scaffold import repair_docs, verify_docs
from pollypm.models import ProviderKind
from pollypm.service_api import PollyPMService
from pollypm.service_api import render_json
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    scan_projects as scan_projects_registry,
)
from pollypm.session_services import (
    attach_existing_session,
    current_session_name,
    probe_session,
    switch_client_to_session,
)
from pollypm.transcript_ingest import start_transcript_ingestion
from pollypm.workers import create_worker_session, launch_worker_session
from pollypm.worktrees import list_worktrees as list_project_worktrees


# wg05 / #242: every `pm ... --help` gains an Examples section so
# first-time users see copy-paste-ready commands for the common flows
# alongside the raw subcommand table. Bullet formatting survives
# typer's rich re-flow (epilog does not).
_APP_HELP = """PollyPM CLI.

Examples (primary flows):

• pm                                — bring up / attach to the PollyPM session
• pm task next                      — find the next queued task to work on
• pm task claim shortlink_gen/1     — claim a queued task (provisions worktree)
• pm worker-start <project>         — spin up a managed worker for a project
• pm projects                       — list registered projects
• pm help worker                    — full worker onboarding guide

Sub-help:  pm task --help, pm session --help, pm project --help, pm plugins --help.
"""

app = typer.Typer(help=_APP_HELP, invoke_without_command=True, no_args_is_help=False)
alert_app = typer.Typer(
    help=(
        "Manage durable alerts.\n\n"
        "Examples:\n\n"
        "• pm alert list                      — show open alerts\n"
        "• pm alert ack <id>                  — acknowledge one\n"
    )
)
session_app = typer.Typer(
    help=(
        "Manage session runtime state.\n\n"
        "Examples:\n\n"
        "• pm session set-status <name> idle       — mark a session idle\n"
        "• pm session set-status <name> working    — mark a session as working\n"
    )
)
heartbeat_app = typer.Typer(
    help=(
        "Run or record heartbeat state.\n\n"
        "Examples:\n\n"
        "• pm heartbeat run                   — run the heartbeat loop once\n"
        "• pm heartbeat status                — show last heartbeat tick\n"
    )
)
issue_app = typer.Typer(
    help=(
        "Manage project issues through the configured backend.\n\n"
        "Examples:\n\n"
        "• pm issue list                      — list issues for the active project\n"
        "• pm issue show <id>                 — print a single issue\n"
    )
)
report_app = typer.Typer(
    help=(
        "Report project status summaries.\n\n"
        "Examples:\n\n"
        "• pm report status                   — summarize all projects\n"
        "• pm report status --project <name>  — summarize one project\n"
    )
)
itsalive_app = typer.Typer(
    help=(
        "Manage itsalive deployments.\n\n"
        "Examples:\n\n"
        "• pm itsalive status                 — show deployment state\n"
        "• pm itsalive deploy                 — deploy the project\n"
    )
)
app.add_typer(alert_app, name="alert")
app.add_typer(session_app, name="session")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(issue_app, name="issue")
app.add_typer(report_app, name="report")
app.add_typer(itsalive_app, name="itsalive")

from pollypm.work.cli import task_app, flow_app
app.add_typer(task_app, name="task")
app.add_typer(flow_app, name="flow")

from pollypm.work.inbox_cli import inbox_app
app.add_typer(inbox_app, name="inbox")

from pollypm.jobs.cli import jobs_app
app.add_typer(jobs_app, name="jobs")

from pollypm.plugin_cli import plugins_app
app.add_typer(plugins_app, name="plugins")

from pollypm.rail_cli import rail_app
app.add_typer(rail_app, name="rail")

from pollypm.plugins_builtin.activity_feed.cli import activity_app
app.add_typer(activity_app, name="activity")

from pollypm.plugins_builtin.morning_briefing.cli import briefing_app
app.add_typer(briefing_app, name="briefing")

from pollypm.plugins_builtin.project_planning.cli import project_app
app.add_typer(project_app, name="project")

from pollypm.memory_cli import memory_app
app.add_typer(memory_app, name="memory")

from pollypm.plugins_builtin.advisor.cli.advisor_cli import advisor_app
app.add_typer(advisor_app, name="advisor")

from pollypm.plugins_builtin.downtime.cli import downtime_app
app.add_typer(downtime_app, name="downtime")


def _session_name_candidates() -> list[str]:
    return ["pollypm", "pollypm-storage-closet"]


def _discover_config_path(config_path: Path) -> Path:
    return resolve_config_path(config_path)


def _config_option_was_explicit() -> bool:
    return any(arg == "--config" or arg.startswith("--config=") for arg in sys.argv[1:])


def _attach_existing_session_without_config() -> bool:
    current_tmux = current_session_name()
    for session_name in _session_name_candidates():
        if not probe_session(session_name):
            continue
        if current_tmux == session_name:
            return True
        if current_tmux:
            raise typer.Exit(code=switch_client_to_session(session_name))
        raise typer.Exit(code=attach_existing_session(session_name))
    return False


def _load_supervisor(config_path: Path):
    """Return a full Supervisor via the service_api facade."""
    return PollyPMService(config_path).load_supervisor()


def _account_label(supervisor, account_name: str) -> str:
    account = supervisor.config.accounts.get(account_name)
    if account is None:
        return account_name
    return account.email or account.name


def _cli_status(msg: str) -> None:
    """Print a status update on its own line."""
    typer.echo(msg)


def _emit_json(payload: object) -> None:
    typer.echo(render_json(payload), nl=False)


def _install_global_pollypm(root_dir: Path) -> tuple[bool, str]:
    result = subprocess.run(
        ["uv", "tool", "install", "--editable", "--reinstall", str(root_dir)],
        cwd=root_dir,
        check=False,
        text=True,
        capture_output=True,
    )
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part).strip()
    return (result.returncode == 0, output)


def _require_pollypm_session(supervisor) -> None:
    current_tmux = supervisor.tmux.current_session_name()
    expected = supervisor.config.project.tmux_session
    allowed = {expected, supervisor.storage_closet_session_name()}
    if current_tmux not in allowed:
        raise typer.BadParameter(
            f"This command must run inside tmux session '{expected}'. Use `pm up` to attach first."
        )


def _first_run_setup_and_launch(config_path: Path) -> None:
    from pollypm.onboarding import run_onboarding
    path = run_onboarding(config_path=config_path, force=False)
    _install_global_pollypm(path.parent)
    up(config_path=path)


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    config_path = _discover_config_path(config_path)
    if ctx.invoked_subcommand is None:
        if not config_path.exists():
            if config_path == DEFAULT_CONFIG_PATH and _attach_existing_session_without_config():
                return
            _first_run_setup_and_launch(config_path=config_path)
            return
        up(config_path=config_path)


@app.command()
def init(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to write the example config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    write_example_config(config_path, force=force)
    typer.echo(f"Wrote config to {config_path}")


@app.command()
def example_config() -> None:
    typer.echo(render_example_config())


_ROLE_GUIDES = {
    "worker": ("docs/worker-guide.md", "Worker onboarding guide"),
}


@app.command("help")
def role_help(
    role: str = typer.Argument(
        ...,
        help="Role whose guide to print. Currently supported: worker.",
    ),
) -> None:
    """Print the canonical guide for a role (worker, ...).

    Role-scoped help surfaces the same content that's auto-injected
    into a role's session prompt. Use this when you're outside a
    managed session and need the playbook.
    """
    role_norm = role.strip().lower()
    entry = _ROLE_GUIDES.get(role_norm)
    if entry is None:
        available = ", ".join(sorted(_ROLE_GUIDES.keys())) or "<none>"
        typer.echo(
            f"No guide registered for role '{role}'. "
            f"Available: {available}.",
            err=True,
        )
        raise typer.Exit(code=1)
    rel_path, title = entry
    # Resolve against the repo root. ``pollypm`` is installed editable
    # during dev; at runtime we prefer the packaged doc if it exists,
    # falling back to the repo copy.
    from importlib.resources import files as _files

    doc_text: str | None = None
    try:
        # Packaged layout: src/pollypm/defaults/worker-guide.md (if we
        # later ship it). For now fall through to the repo docs dir.
        candidate = _files("pollypm").joinpath(f"../../{rel_path}")
        if candidate.is_file():
            doc_text = candidate.read_text()
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    if doc_text is None:
        # Walk up from this file to find the project root's docs/ dir.
        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / rel_path
            if candidate.is_file():
                doc_text = candidate.read_text()
                break
    if doc_text is None:
        typer.echo(
            f"Could not locate {rel_path} on disk. "
            f"The guide exists in the PollyPM repo at that path.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(doc_text)


@app.command()
def onboard(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to write the onboarding config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
    from pollypm.onboarding import run_onboarding
    path = run_onboarding(config_path=config_path, force=force)
    installed, install_output = _install_global_pollypm(path.parent)
    typer.echo("")
    typer.echo(f"Wrote onboarding config to {path}")
    if installed:
        typer.echo("Installed global commands: `pollypm` and `pm`.")
    else:
        typer.echo("Could not auto-install the global `pollypm` command.")
        if install_output:
            typer.echo(install_output)
    typer.echo("Next step: run `pollypm up` or `uv run pm up` to create or attach to the PollyPM tmux session.")


@app.command()
def doctor(
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of the human checklist.",
    ),
    fix: bool = typer.Option(
        False, "--fix", help="Auto-fix the safe subset (missing dirs, stale panes).",
    ),
) -> None:
    """Validate a new-user environment end-to-end.

    Runs a battery of fast checks (<5s on a healthy system) covering
    system prerequisites, install state, plugins, migrations,
    filesystem, tmux, and network reachability. Every failure is
    reported with three pieces of information: what's wrong, why
    PollyPM needs it, and the exact fix command.
    """
    from pollypm.doctor import apply_fixes, render_human, render_json, run_checks

    report = run_checks()
    if fix:
        fix_results = apply_fixes(report)
        if fix_results:
            for name, success, message in fix_results:
                glyph = "fixed" if success else "fix failed"
                typer.echo(f"  [{glyph}] {name}: {message}")
            # Re-run checks so the final output reflects the fixes.
            report = run_checks()
    if json_output:
        typer.echo(render_json(report))
    else:
        typer.echo(render_human(report))
    raise typer.Exit(code=0 if report.ok else 1)


@app.command()
def accounts(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    for account in list_account_statuses(config_path):
        typer.echo(
            f"- {account.key}: {account.email} [{account.provider.value}] "
            f"logged_in={'yes' if account.logged_in else 'no'} health={account.health} "
            f"usage={account.usage_summary} isolation={account.isolation_status}"
        )
        typer.echo(
            f"  isolation_summary={account.isolation_summary} "
            f"auth_storage={account.auth_storage} profile_root={account.profile_root or '-'}"
        )
        if account.isolation_recommendation:
            typer.echo(f"  isolation_recommendation={account.isolation_recommendation}")
        if account.available_at or account.access_expires_at or account.reason:
            typer.echo(
                f"  reason={account.reason or '-'} available_at={account.available_at or '-'} "
                f"access_expires_at={account.access_expires_at or '-'}"
            )


@app.command("account-doctor")
def account_doctor(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    config = load_config(config_path)
    statuses = list_account_statuses(config_path)
    if not statuses:
        typer.echo("No configured accounts.")
        return
    for account in statuses:
        typer.echo(f"[{account.key}]")
        typer.echo(f"provider = {account.provider.value}")
        typer.echo(f"runtime = {config.accounts[account.key].runtime.value}")
        typer.echo(f"logged_in = {'yes' if account.logged_in else 'no'}")
        typer.echo(f"isolation_status = {account.isolation_status}")
        typer.echo(f"auth_storage = {account.auth_storage}")
        typer.echo(f"profile_root = {account.profile_root or '-'}")
        typer.echo(f"summary = {account.isolation_summary}")
        if account.isolation_recommendation:
            typer.echo(f"recommendation = {account.isolation_recommendation}")
        typer.echo("")


@app.command("refresh-usage")
def refresh_usage(
    account: str = typer.Argument(..., help="Account key or email."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    status = probe_account_usage(config_path, account)
    typer.echo(
        f"{status.key}: plan={status.plan} health={status.health} "
        f"usage={status.usage_summary}"
    )


@app.command("tokens-sync")
def tokens_sync(
    account: str | None = typer.Option(None, "--account", help="Optional account key or email to limit scanning."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    count = service.sync_token_ledger(account=account)
    typer.echo(f"Synced {count} transcript token sample(s).")


@app.command("tokens")
def tokens(
    limit: int = typer.Option(10, "--limit", min=1, max=100, help="Maximum rows to show."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    rows = service.recent_token_usage(limit=limit)
    if not rows:
        typer.echo("No token usage recorded yet.")
        return
    for row in rows:
        typer.echo(
            f"- {row.hour_bucket} {row.project_key} {row.account_name} {row.provider}/{row.model_name}: {row.tokens_used} tokens"
        )


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
    import traceback
    from datetime import datetime
    crash_log = config_path.parent / "cockpit_crash.log"
    debug_log = config_path.parent / "cockpit_debug.log"
    try:
        with open(debug_log, "a") as dl:
            dl.write(f"\n--- START {datetime.now().isoformat()} ---\n")
        from pollypm.cockpit_ui import PollyCockpitApp
        PollyCockpitApp(config_path).run(mouse=True)
        with open(debug_log, "a") as dl:
            dl.write(f"--- CLEAN EXIT {datetime.now().isoformat()} ---\n")
    except Exception:
        with open(crash_log, "a") as f:
            f.write(f"\n--- {datetime.now().isoformat()} ---\n")
            traceback.print_exc(file=f)
        with open(debug_log, "a") as dl:
            dl.write(f"--- CRASH {datetime.now().isoformat()} ---\n")
            traceback.print_exc(file=dl)
        raise


@app.command("cockpit-pane")
def cockpit_pane(
    kind: str = typer.Argument(..., help="Pane type: inbox, settings, or project."),
    target: str | None = typer.Argument(None, help="Optional project key for project panes."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
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
        PollyInboxApp(config_path).run(mouse=True)
        return
    if kind == "workers":
        from pollypm.cockpit_ui import PollyWorkerRosterApp
        PollyWorkerRosterApp(config_path).run(mouse=True)
        return
    if kind == "issues" and target:
        from pollypm.cockpit_ui import PollyTasksApp
        PollyTasksApp(config_path, target).run(mouse=True)
        return
    if kind == "activity":
        # Live Activity Feed (lf03) — Textual app owned by the plugin
        # so the cockpit pane doesn't need to know its internals.
        from pollypm.plugins_builtin.activity_feed.cockpit.feed_panel import (
            ActivityFeedApp,
        )

        ActivityFeedApp(config_path).run(mouse=True)
        return
    if kind == "project" and target:
        # Per-project dashboard — beautiful Textual screen, replaces the
        # read-only Static text dump. See issue #245.
        from pollypm.cockpit_ui import PollyProjectDashboardApp
        PollyProjectDashboardApp(config_path, target).run(mouse=True)
        return
    from pollypm.cockpit_ui import PollyCockpitPaneApp
    PollyCockpitPaneApp(config_path, kind, target).run(mouse=True)


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
        False, "--skip-plan",
        help=(
            "Suppress the project_planning auto-fire for this project. "
            "See `[planner] auto_on_project_created` in pollypm.toml for "
            "the global switch."
        ),
    ),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    # Detect whether this invocation is registering a *new* project
    # versus re-touching an already-known path — only genuinely-new
    # projects should fire the `project.created` observer chain (issue
    # #255 part (a)). ``register_project`` silently returns the existing
    # entry when the path matches, so we have to check the pre-state.
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
        # If the config can't be loaded (e.g. first-run), treat as new.
        was_preexisting = False

    project = register_project(config_path, repo_path, name=name)
    typer.echo(f"Registered project {project.name or project.key} at {project.path}")

    # Part (a): symmetrical `project.created` emission. ``pm project new``
    # already emits this; ``pm add-project`` historically did not, which
    # caused live-observed regressions where Polly used her native LLM
    # planning instead of the architect plugin.
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
                    # the project_planning observer can honour the
                    # opt-out without the CLI needing to know about the
                    # plugin. See plugin.py::_on_project_created.
                    "skip_plan": bool(skip_plan),
                },
                metadata={
                    "source": "pm add-project",
                    # Forward the effective config path so observers
                    # that need to read config (e.g. auto-fire gate)
                    # don't have to guess at DEFAULT_CONFIG_PATH. Tests
                    # rely on this to drive a non-default config.
                    "config_path": str(config_path),
                },
            )
        except Exception:  # noqa: BLE001
            # Observers are best-effort — never block registration on a
            # plugin crash. The same guard is in ``pm project new``.
            pass

    if not skip_import:
        typer.echo("Importing project history (transcripts, git, files)...")
        from pollypm.history_import import import_project_history
        try:
            result = import_project_history(
                project.path, project.name or project.key, skip_interview=True,
            )
            typer.echo(
                f"Import complete: {result.sources_found} sources, "
                f"{result.timeline_events} events, {result.docs_generated} docs generated"
            )
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"History import failed (project still registered): {exc}")


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
        project.path, project.name or project_key, skip_interview=True,
    )
    typer.echo(
        f"Import complete:\n"
        f"  Sources discovered: {result.sources_found}\n"
        f"  Timeline events: {result.timeline_events}\n"
        f"  Docs generated: {result.docs_generated}\n"
        f"  Provider transcripts copied: {result.provider_transcripts_copied}"
    )
    if result.interview_questions:
        typer.echo(f"\nGenerated {len(result.interview_questions)} review question(s).")
        for q in result.interview_questions[:5]:
            typer.echo(f"  - {q}")


@app.command("init-tracker")
def init_tracker(
    project: str = typer.Argument(..., help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    tracked = enable_tracked_project(config_path, project)
    typer.echo(f"Enabled tracked-project mode for {tracked.name or tracked.key}")


@issue_app.command("list")
def issue_list(
    project: str = typer.Option(..., "--project", help="Project key."),
    state: list[str] | None = typer.Option(None, "--state", help="Optional tracker state filter."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    tasks = service.list_tasks(project, states=state)
    if not tasks:
        typer.echo("No issues found.")
        return
    for task in tasks:
        typer.echo(f"{task.task_id} [{task.state}] {task.title}")


@issue_app.command("info")
def issue_info(
    task_id: str = typer.Argument(..., help="Issue id."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    task = service.get_task(project, task_id)
    typer.echo(f"{task.task_id} [{task.state}] {task.title}")


@issue_app.command("next")
def issue_next(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    task = service.next_available_task(project)
    if task is None:
        typer.echo("No ready issue found.")
        return
    typer.echo(f"{task.task_id} [{task.state}] {task.title}")


@issue_app.command("history")
def issue_history(
    task_id: str = typer.Argument(..., help="Issue id."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    entries = service.task_history(project, task_id)
    if not entries:
        typer.echo("No history found.")
        return
    for entry in entries:
        typer.echo(entry)


@issue_app.command("create")
def issue_create(
    project: str = typer.Option(..., "--project", help="Project key."),
    title: str = typer.Option(..., "--title", help="Issue title."),
    body: str = typer.Option("", "--body", help="Issue body."),
    state: str = typer.Option("01-ready", "--state", help="Initial tracker state."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    task = service.create_task(project, title=title, body=body, state=state)
    typer.echo(f"Created issue {task.task_id} [{task.state}] {task.title}")


@issue_app.command("transition")
def issue_transition(
    task_id: str = typer.Argument(..., help="Issue id."),
    to_state: str = typer.Argument(..., help="Destination tracker state."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    try:
        task = service.move_task(project, task_id, to_state=to_state)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Moved issue {task.task_id} to {task.state}")


@issue_app.command("comment")
def issue_comment(
    task_name: str = typer.Argument(..., help="Issue id or note target."),
    text: str = typer.Option(..., "--text", help="Comment text."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    path = service.append_task_note(project, task_name, text=text)
    typer.echo(f"Updated {path}")


@issue_app.command("handoff")
def issue_handoff(
    task_name: str = typer.Argument(..., help="Issue id or note target."),
    what_done: str = typer.Option(..., "--done", help="Summary of what was completed."),
    how_to_test: str = typer.Option(..., "--test", help="How to verify the work."),
    branch_or_pr: str = typer.Option("", "--branch-or-pr", help="Branch name or PR link for review."),
    deviations: str = typer.Option("", "--deviations", help="Any spec deviations and why."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    path = service.append_task_handoff(
        project,
        task_name,
        what_done=what_done,
        how_to_test=how_to_test,
        branch_or_pr=branch_or_pr,
        deviations=deviations,
    )
    typer.echo(f"Updated {path}")


@issue_app.command("approve")
def issue_approve(
    task_id: str = typer.Argument(..., help="Issue id."),
    summary: str = typer.Option(..., "--summary", help="Review summary."),
    verification: str = typer.Option(..., "--verification", help="Independent verification performed."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    try:
        task = service.review_task(
            project,
            task_id,
            approved=True,
            summary=summary,
            verification=verification,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Approved issue {task.task_id} to {task.state}")


@issue_app.command("request-changes")
def issue_request_changes(
    task_id: str = typer.Argument(..., help="Issue id."),
    summary: str = typer.Option(..., "--summary", help="Review summary."),
    verification: str = typer.Option(..., "--verification", help="Independent verification performed."),
    changes: str = typer.Option(..., "--changes", help="Specific requested changes."),
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    try:
        task = service.review_task(
            project,
            task_id,
            approved=False,
            summary=summary,
            verification=verification,
            changes_requested=changes,
        )
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Returned issue {task.task_id} to {task.state}")


@issue_app.command("counts")
def issue_counts(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    counts = service.task_state_counts(project)
    for state, count in counts.items():
        typer.echo(f"{state}: {count}")


@issue_app.command("report")
def issue_report(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    issue_counts(project=project, config_path=config_path)


@report_app.command("status")
def report_status(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    issue_counts(project=project, config_path=config_path)


@itsalive_app.command("deploy")
def itsalive_deploy(
    project: str = typer.Option(..., "--project", help="Project key."),
    subdomain: str | None = typer.Option(None, "--subdomain", help="itsalive subdomain for first deploy."),
    email: str | None = typer.Option(None, "--email", help="Email for first deploy if not already verified."),
    publish_dir: str = typer.Option(".", "--dir", help="Directory to deploy relative to the project root."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    outcome = service.itsalive_deploy(project_key=project, subdomain=subdomain, email=email, publish_dir=publish_dir)
    typer.echo(f"status={outcome.status}")
    typer.echo(f"message={outcome.message}")
    typer.echo(f"subdomain={outcome.subdomain}")
    if outcome.url:
        typer.echo(f"url={outcome.url}")
    if outcome.pending_path:
        typer.echo(f"pending={outcome.pending_path}")
    if outcome.expires_at:
        typer.echo(f"expires_at={outcome.expires_at}")


@itsalive_app.command("status")
def itsalive_status(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    items = service.itsalive_pending(project_key=project)
    if not items:
        typer.echo("No pending itsalive deployments.")
        return
    for item in items:
        typer.echo(f"{item.subdomain} deploy_id={item.deploy_id} expires_at={item.expires_at} email={item.email}")


@itsalive_app.command("sweep")
def itsalive_sweep(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    outcomes = service.itsalive_sweep(project_key=project)
    if not outcomes:
        typer.echo("No itsalive deployment updates.")
        return
    for outcome in outcomes:
        typer.echo(f"{outcome.subdomain}: {outcome.status} {outcome.message}")
        if outcome.url:
            typer.echo(f"  {outcome.url}")


@issue_app.command("validate")
def issue_validate(
    project: str = typer.Option(..., "--project", help="Project key."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    service = PollyPMService(config_path)
    result = service.validate_task_backend(project)
    if getattr(result, "passed", False):
        typer.echo("Task backend validation passed.")
    else:
        typer.echo("Task backend validation failed.")
    for check in getattr(result, "checks", []):
        typer.echo(f"check: {check}")
    for error in getattr(result, "errors", []):
        typer.echo(f"error: {error}")
    if not getattr(result, "passed", False):
        raise typer.Exit(code=1)



@app.command("costs")
def costs(
    project: str | None = typer.Option(None, "--project", help="Filter by project key."),
    days: int = typer.Option(7, "--days", help="Look back N days."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    """Show token usage by project for the last N days."""
    config = load_config(config_path)
    from pollypm.storage.state import StateStore
    store = StateStore(config.project.state_db)
    rows = store.execute(
        """
        SELECT project_key, SUM(tokens_used) as total,
               SUM(cache_read_tokens) as cache_total,
               COUNT(DISTINCT substr(hour_bucket, 1, 10)) as days_active
        FROM token_usage_hourly
        WHERE hour_bucket >= date('now', ?)
        GROUP BY project_key
        ORDER BY total DESC
        """,
        (f"-{days} days",),
    ).fetchall()
    store.close()
    if not rows:
        typer.echo("No token usage data.")
        return
    typer.echo(f"Token usage (last {days} days):\n")
    total_all = 0
    cache_all = 0
    for row in rows:
        proj_key, total, cache, days_active = row[0], int(row[1]), int(row[2] or 0), int(row[3])
        if project and proj_key != project:
            continue
        cache_str = f" + {cache:,} cached" if cache else ""
        typer.echo(f"  {proj_key}: {total:,} tokens{cache_str} ({days_active} active day(s))")
        total_all += total
        cache_all += cache
    cache_str = f" + {cache_all:,} cached" if cache_all else ""
    typer.echo(f"\n  Total: {total_all:,} tokens{cache_str}")


@app.command("worktrees")
def worktrees(
    project: str | None = typer.Option(None, "--project", help="Optional project key filter."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    items = list_project_worktrees(config_path, project)
    if not items:
        typer.echo("No tracked worktrees.")
        return
    for item in items:
        typer.echo(
            f"- {item.project_key} {item.lane_kind}/{item.lane_key}: {item.path} "
            f"[{item.branch}] status={item.status}"
        )


@app.command()
def add_account(
    provider: str = typer.Argument(..., help="Provider to add: codex or claude."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    provider_kind = ProviderKind(provider.lower())
    key, email = add_account_via_login(config_path, provider_kind)
    typer.echo(f"Added {email} as {key}")


@app.command()
def relogin(
    account: str = typer.Argument(..., help="Account key or email."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    key, email = relogin_account(config_path, account)
    typer.echo(f"Re-authenticated {email} ({key})")


@app.command()
def remove_account(
    account: str = typer.Argument(..., help="Account key or email."),
    delete_home: bool = typer.Option(False, "--delete-home", help="Also delete the isolated account home."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    key, email = remove_account_entry(config_path, account, delete_home=delete_home)
    typer.echo(f"Removed {email} ({key})")


@app.command()
def up(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    config_path = _discover_config_path(config_path)
    if not config_path.exists():
        if config_path == DEFAULT_CONFIG_PATH and _attach_existing_session_without_config():
            return
        typer.echo(f"Config not found at {config_path}. Starting onboarding.")
        onboard(config_path=config_path, force=False)
        return
    supervisor = _load_supervisor(config_path)
    # CoreRail owns startup orchestration — it drives plugin host load,
    # state store readiness, and Supervisor boot (which runs ensure_layout,
    # ensure_heartbeat_schedule, and ensure_knowledge_extraction_schedule).
    # Test harnesses that mock Supervisor without a core_rail fall back
    # to the legacy per-call path below.
    if hasattr(supervisor, "core_rail"):
        supervisor.core_rail.start()
    else:  # pragma: no cover - back-compat for mocked Supervisors in tests
        supervisor.ensure_layout()
    if all(hasattr(supervisor.config, field) for field in ("project", "accounts", "projects")) and hasattr(
        supervisor.config.project, "base_dir"
    ):
        start_transcript_ingestion(supervisor.config)
    session_name = supervisor.config.project.tmux_session
    current_tmux = supervisor.tmux.current_session_name()
    created = False

    if not supervisor.tmux.has_session(session_name):
        storage_alive = supervisor.tmux.has_session(supervisor.storage_closet_session_name())
        if storage_alive:
            supervisor.tmux.create_session(
                session_name, supervisor.CONSOLE_WINDOW, supervisor.console_command(), remain_on_exit=False,
            )
            supervisor.tmux.set_window_option(f"{session_name}:{supervisor.CONSOLE_WINDOW}", "allow-passthrough", "on")
            supervisor.tmux.set_window_option(f"{session_name}:{supervisor.CONSOLE_WINDOW}", "window-size", "latest")
            supervisor.tmux.set_window_option(f"{session_name}:{supervisor.CONSOLE_WINDOW}", "aggressive-resize", "on")
            created = True
            typer.echo(f"Restored tmux session {session_name} (storage-closet still alive)")
        else:
            try:
                controller_account = supervisor.bootstrap_tmux(skip_probe=True, on_status=_cli_status)
            except RuntimeError as exc:
                raise typer.BadParameter(str(exc)) from exc
            created = True
            controller = supervisor.config.accounts[controller_account]
            typer.echo(
                f"Created tmux session {session_name} with controller "
                f"{controller.email or controller_account} [{controller.provider.value}]"
            )
    else:
        supervisor.ensure_console_window()

    # Back-compat: when CoreRail wasn't available (mocked Supervisor),
    # run the schedule ensures explicitly so test harnesses and any
    # third-party Supervisor fakes still see the expected side effects.
    if not hasattr(supervisor, "core_rail"):  # pragma: no cover
        supervisor.ensure_heartbeat_schedule()
        if hasattr(supervisor, "ensure_knowledge_extraction_schedule"):
            supervisor.ensure_knowledge_extraction_schedule()

    # Set up the cockpit layout (split panes) BEFORE the TUI starts,
    # then launch the TUI into the rail pane.
    from pollypm.cockpit import CockpitRouter
    router = CockpitRouter(config_path)
    try:
        router.ensure_cockpit_layout()
        import time; time.sleep(0.3)  # let tmux settle after the split
        supervisor.start_cockpit_tui(session_name)
    except Exception:  # noqa: BLE001
        pass  # layout will be fixed on next cockpit launch

    if current_tmux == session_name:
        supervisor.focus_console()
        typer.echo(f"Already inside tmux session {session_name}")
        return

    if current_tmux:
        # Don't yank the user's tmux client to pollypm — just report success.
        # The user can switch manually with: tmux switch-client -t pollypm
        typer.echo(f"PollyPM is running. Attach with: tmux switch-client -t {session_name}")
        return

    raise typer.Exit(code=supervisor.tmux.attach_session(session_name))


@app.command()
def launch(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    up(config_path=config_path)


@app.command()
def reset(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt."),
) -> None:
    """Kill all PollyPM tmux sessions (cockpit + storage closet). Use `pm up` to restart."""
    config_path = _discover_config_path(config_path)
    if not config_path.exists():
        typer.echo(f"Config not found at {config_path}.")
        raise typer.Exit(code=1)
    supervisor = _load_supervisor(config_path)
    session_name = supervisor.config.project.tmux_session
    storage_name = supervisor.storage_closet_session_name()
    sessions_to_kill = [
        name for name in [session_name, storage_name]
        if supervisor.tmux.has_session(name)
    ]
    if not sessions_to_kill:
        typer.echo("No PollyPM tmux sessions found.")
        return
    if not force:
        names = ", ".join(sessions_to_kill)
        typer.confirm(
            f"This will kill all PollyPM sessions ({names}). Continue?",
            abort=True,
        )
    supervisor.shutdown_tmux()
    # Clean up all transient state so pm up starts fresh
    jobs_path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
    jobs_path.unlink(missing_ok=True)
    cockpit_state = supervisor.config.project.base_dir / "cockpit_state.json"
    cockpit_state.unlink(missing_ok=True)
    # Clear stale leases — mounted cockpit leases would block recovery on restart
    try:
        supervisor.store.execute("DELETE FROM leases")
        supervisor.store.execute("DELETE FROM session_runtime")
        supervisor.store.execute("DELETE FROM alerts WHERE status = 'open'")
        supervisor.store.commit()
    except Exception:  # noqa: BLE001
        pass
    typer.echo(f"Killed {len(sessions_to_kill)} session(s): {', '.join(sessions_to_kill)}")


@app.command()
def status(
    session_name: str | None = typer.Argument(None, help="Optional session name from config."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    if not _config_option_was_explicit():
        config_path = _discover_config_path(config_path)
    payload = PollyPMService(config_path).session_status(session_name)
    sessions = payload["sessions"]
    if session_name is not None and not sessions:
        raise typer.BadParameter(f"Unknown session: {session_name}")
    if json_output:
        _emit_json(payload)
        return
    if not sessions:
        typer.echo("No sessions configured.")
        return
    typer.echo(f"Config: {payload['config_path']}")
    for item in sessions:
        typer.echo(
            f"- {item['name']}: status={item['status']} running={'yes' if item['running'] else 'no'} "
            f"alerts={item['alert_count']} lease={item['lease_owner'] or '-'} "
            f"project={item['project']} role={item['role']}"
        )
        if item["last_failure_message"]:
            typer.echo(f"  reason={item['last_failure_message']}")
    for error in payload["errors"]:
        typer.echo(f"- error: {error}")


@app.command()
def plan(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    for launch in supervisor.plan_launches():
        typer.echo(f"[{launch.session.name}]")
        typer.echo(f"window = {launch.window_name}")
        typer.echo(f"log = {launch.log_path}")
        typer.echo(f"command = {launch.command}")
        typer.echo("")


@heartbeat_app.callback(invoke_without_command=True)
def heartbeat(
    ctx: typer.Context,
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    snapshot_lines: int = typer.Option(200, "--snapshot-lines", min=20, help="Lines to capture per pane."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    supervisor = _load_supervisor(config_path)
    alerts = supervisor.run_heartbeat(snapshot_lines=snapshot_lines)
    # Fallback rail driver ("suspenders" to the cockpit ticker's "belt"):
    # when no persistent host is running, the every-minute cron still
    # advances recurring roster handlers (task_assignment.sweep,
    # transcript.ingest, work.progress_sweep, etc.). If the cockpit is
    # running, the CoreRail's HeartbeatRail is already booted and this
    # is just one extra tick — no harm. See issue #268 Gap A.
    _tick_core_rail_if_available(supervisor)
    if json_output:
        _emit_json({"alerts": alerts})
        return
    typer.echo(f"Heartbeat completed. Open alerts: {len(alerts)}")
    for alert in alerts:
        typer.echo(f"- {alert.severity} {alert.session_name}/{alert.alert_type}#{alert.alert_id}: {alert.message}")


def _tick_core_rail_if_available(supervisor) -> None:
    """Tick the process-wide HeartbeatRail if the supervisor exposes one.

    No-ops silently when the rail isn't available (legacy supervisors,
    mocked test harnesses, boot failures). Swallows tick exceptions so
    a bad roster entry can't break the session-health heartbeat that
    already ran above.
    """
    rail_getter = getattr(supervisor, "core_rail", None)
    if rail_getter is None:
        return
    try:
        # CoreRail.start() is idempotent and ensures the HeartbeatRail
        # is booted. This is a transient driver — the worker pool drains
        # anything we enqueue synchronously over the next few seconds.
        rail_getter.start()
        heartbeat_rail = rail_getter.get_heartbeat_rail()
        if heartbeat_rail is None:
            return
        heartbeat_rail.tick()
    except Exception:  # noqa: BLE001
        # Non-fatal — session-health sweep already succeeded above.
        logger.debug("pm heartbeat: core rail tick failed", exc_info=True)


@app.command()
def alerts(
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    items = PollyPMService(config_path).list_alerts()
    if not items:
        typer.echo("No open alerts.")
        return
    if json_output:
        _emit_json({"alerts": items})
        return
    for alert in items:
        typer.echo(f"- #{alert.alert_id} {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")


@app.command("failover")
def failover(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    """Show failover configuration: controller account and failover order."""
    config = load_config(config_path)
    typer.echo(f"Controller: {config.pollypm.controller_account}")
    typer.echo(f"Failover enabled: {'yes' if config.pollypm.failover_enabled else 'no'}")
    if config.pollypm.failover_accounts:
        typer.echo("Failover order:")
        for i, name in enumerate(config.pollypm.failover_accounts, 1):
            account = config.accounts.get(name)
            label = f"{account.email} [{account.provider.value}]" if account else name
            typer.echo(f"  {i}. {label}")
    else:
        typer.echo("No failover accounts configured.")


@app.command("debug")
def debug_command(
    session: str | None = typer.Option(None, "--session", "-s", help="Filter to a specific session."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    """Show diagnostic info: open alerts, session states, recent events. Works outside tmux."""
    supervisor = _load_supervisor(config_path)

    # Alerts
    all_alerts = supervisor.open_alerts()
    alerts_list = [a for a in all_alerts if session is None or a.session_name == session]
    typer.echo(f"Open alerts: {len(alerts_list)}")
    for alert in alerts_list:
        typer.echo(f"  {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")

    # Sessions
    typer.echo("")
    launches = supervisor.plan_launches()
    windows = supervisor.window_map()
    for launch in launches:
        if session is not None and launch.session.name != session:
            continue
        window = windows.get(launch.window_name)
        if window is None:
            state = "not running"
        elif window.pane_dead:
            state = "dead"
        else:
            state = f"running ({window.pane_current_command})"
        typer.echo(f"  {launch.session.name}: {state} [{launch.session.provider.value}/{launch.account.name}]")

    # Recent events
    typer.echo("")
    events_list = supervisor.store.recent_events(limit=5)
    if session is not None:
        events_list = [e for e in events_list if e.session_name == session]
    typer.echo(f"Recent events: {len(events_list)}")
    for event in events_list[:5]:
        typer.echo(f"  {event.created_at} {event.session_name}/{event.event_type}: {event.message}")


@app.command()
def events(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum number of events to show."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    items = supervisor.store.recent_events(limit=limit)
    if not items:
        typer.echo("No events recorded.")
        return
    for event in items:
        typer.echo(f"- {event.created_at} {event.session_name}/{event.event_type}: {event.message}")


@app.command()
def claim(
    session_name: str = typer.Argument(..., help="Session name from config."),
    owner: str = typer.Option("human", "--owner", help="Lease owner label."),
    note: str = typer.Option("", "--note", help="Optional note for the lease."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    supervisor.claim_lease(session_name, owner, note)
    typer.echo(f"Lease set on {session_name} for {owner}")


@app.command()
def release(
    session_name: str = typer.Argument(..., help="Session name from config."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    supervisor.release_lease(session_name)
    typer.echo(f"Lease released for {session_name}")


@app.command()
def send(
    session_name: str = typer.Argument(..., help="Session name from config."),
    text: str = typer.Argument(..., help="Text to send into the tmux pane."),
    owner: str = typer.Option("pollypm", "--owner", help="Sender label for lease checks."),
    force: bool = typer.Option(False, "--force", help="Bypass a conflicting lease."),
    no_enter: bool = typer.Option(False, "--no-enter", help="Do not send Enter after the text."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    # Block pm send to workers at the CLI level UNLESS --force is set.
    # The default nudges the operator to use the task system (audit trail
    # + reply path). --force is the escape hatch for when the auto-pickup
    # path is broken and a human operator needs to push a command through
    # directly — e.g., nudging a stuck worker. #261.
    session_cfg = supervisor.config.sessions.get(session_name)
    if session_cfg and session_cfg.role == "worker" and not force:
        project = session_cfg.project or session_name.replace("worker_", "", 1)
        typer.echo(
            f"Blocked: dispatch work through the task system.\n"
            f"  pm task create \"Title\" -p {project} -d \"description\" "
            f"-f standard -r worker=worker -r reviewer=polly\n"
            f"  pm task queue {project}/<number>\n"
            f"\n"
            f"The worker picks up queued tasks automatically.\n"
            f"If the auto-pickup path is broken and you need to nudge "
            f"this worker directly, re-run with --force."
        )
        raise typer.Exit(code=1)
    try:
        supervisor.send_input(session_name, text, owner=owner, force=force, press_enter=not no_enter)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        _emit_json(
            {
                "session_name": session_name,
                "owner": owner,
                "text": text,
                "press_enter": not no_enter,
                "forced": force,
            }
        )
        return
    typer.echo(f"Sent input to {session_name}")


@app.command()
def notify(
    subject: str = typer.Argument(..., help="Short title for the inbox item."),
    body: str = typer.Argument(..., help="Message body. Pass '-' to read from stdin."),
    actor: str = typer.Option("polly", "--actor", help="Who is posting the notification."),
    project: str = typer.Option(
        "inbox", "--project", "-p",
        help="Project namespace for the notification task (default: 'inbox').",
    ),
    priority: str = typer.Option(
        "auto", "--priority",
        help=(
            "Tier: 'immediate' surfaces in the inbox now; 'digest' stages "
            "silently and rolls up at the next milestone boundary; "
            "'silent' only records an audit event. 'auto' (default) "
            "infers the tier from subject/body keywords — falling back "
            "to 'immediate' when ambiguous."
        ),
    ),
    milestone: str = typer.Option(
        "",
        "--milestone",
        help=(
            "Optional milestone key for digest bucketing "
            "(e.g. 'milestones/02-core-features'). Leave blank to let "
            "milestone detection classify at flush time."
        ),
    ),
    labels: list[str] = typer.Option(
        None,
        "--label",
        help=(
            "Attach a label to the created inbox task. Repeatable. "
            "Used by typed flows like plan_review "
            "(e.g. --label plan_review --label 'plan_task:key/1' "
            "--label 'explainer:/abs/path/plan-review.html')."
        ),
    ),
    requester: str = typer.Option(
        "user",
        "--requester",
        help=(
            "Role assigned as the task's requester. Defaults to 'user' "
            "(normal user inbox). Pass 'polly' to route to Polly's "
            "inbox instead (fast-track plan_review)."
        ),
    ),
    db: str = typer.Option(
        ".pollypm/state.db", "--db",
        help="Path to SQLite database (default: same resolution as `pm inbox`).",
    ),
) -> None:
    """Create a work-service inbox item for the human user.

    This is the canonical escalation channel referenced by the operator
    runbook and control prompts. Polly (or any agent) uses ``pm notify``
    to reach the user when something needs attention — a blocker, a
    completed deliverable, a status update.

    The notification is stored as a work-service task on the ``chat``
    flow with ``roles.requester=user``, so it appears in ``pm inbox``
    immediately.

    Examples:

    • pm notify "Deploy blocked" "Needs verification email click."
    • pm notify "Done: homepage rewrite" "Review at https://…"
    • echo "long body" | pm notify "Subject" -
    • pm notify "Plan ready" "Review the plan" --priority immediate \\
          --label plan_review --label "plan_task:demo/5" \\
          --label "explainer:/abs/path/reports/plan-review.html"
    """
    if not subject.strip():
        typer.echo("Error: subject must not be empty.", err=True)
        raise typer.Exit(code=1)

    if body == "-":
        body = sys.stdin.read()
    if not body.strip():
        typer.echo(
            "Error: body must not be empty (pass '-' to read from stdin).",
            err=True,
        )
        raise typer.Exit(code=1)

    # Resolve priority. 'auto' means "classify by keyword".
    from pollypm import notification_staging as _ns

    requested = (priority or "auto").strip().lower()
    if requested == "auto":
        resolved_priority = _ns.classify_priority(subject, body)
    else:
        try:
            resolved_priority = _ns.validate_priority(requested)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    # Use the same work-service entry point pm inbox reads through, so
    # the notification lands in the identical DB and surfaces immediately.
    from pollypm.plugins_builtin.activity_feed.summaries import activity_summary
    from pollypm.storage.state import StateStore
    from pollypm.work.cli import _resolve_db_path, _svc

    milestone_key = milestone.strip() or None

    # Silent tier: audit only, no inbox task, no staging row.
    if resolved_priority == "silent":
        db_path = _resolve_db_path(db, project=project)
        store = StateStore(db_path)
        try:
            store.record_event(
                actor,
                "inbox.message.silent",
                activity_summary(
                    summary=f"{actor} (silent): {subject}",
                    severity="routine",
                    verb="recorded",
                    subject=subject,
                    project=project,
                    body=body,
                ),
            )
        finally:
            store.close()
        typer.echo("silent")
        return

    # Digest tier: stage the row, no inbox task.
    if resolved_priority == "digest":
        svc = _svc(db, project=project)
        try:
            payload = {
                "subject": subject,
                "body": body,
                "actor": actor,
                "project": project,
            }
            staging_id = _ns.stage_notification(
                svc._conn,  # type: ignore[attr-defined]
                project=project,
                subject=subject,
                body=body,
                actor=actor,
                priority="digest",
                milestone_key=milestone_key,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"Failed to stage digest notification: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        db_path = _resolve_db_path(db, project=project)
        store = StateStore(db_path)
        try:
            store.record_event(
                actor,
                "inbox.message.staged",
                activity_summary(
                    summary=f"{actor} (digest): {subject}",
                    severity="routine",
                    verb="staged",
                    subject=subject,
                    project=project,
                    staging_id=staging_id,
                    milestone_key=milestone_key,
                    body=body,
                ),
            )
        finally:
            store.close()
        typer.echo(f"digest:{staging_id}")
        return

    # Immediate tier (default) — create an inbox task as before.
    svc = _svc(db, project=project)
    # Normalise the requester role. ``user`` → normal inbox (default).
    # ``polly`` → fast-track plan_review: lands in Polly's inbox instead
    # of the user's. Anything else is rejected so we don't silently
    # mis-route escalations.
    requester_role = (requester or "user").strip().lower()
    if requester_role not in ("user", "polly"):
        typer.echo(
            f"Error: --requester must be 'user' or 'polly' (got {requester!r}).",
            err=True,
        )
        raise typer.Exit(code=1)
    label_list = [label for label in (labels or []) if label and label.strip()]
    try:
        task = svc.create(
            title=subject,
            description=body,
            type="task",
            project=project,
            flow_template="chat",
            # requester=user makes _roles_match_user() trip in the
            # inbox_view filter — the canonical mark for "user must
            # look at this". requester=polly routes to Polly's inbox
            # (see #297 fast-track plan review).
            roles={"requester": requester_role, "operator": actor},
            priority="normal",
            created_by=actor,
            labels=label_list or None,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to create inbox task: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Audit event — mirrors the legacy inbox_message_created shape so the
    # activity feed projector/consumers see the notification.
    db_path = _resolve_db_path(db, project=project)
    store = StateStore(db_path)
    try:
        store.record_event(
            actor,
            "inbox.message.created",
            activity_summary(
                summary=f"{actor} -> user: {subject}",
                severity="recommendation",
                verb="notified",
                subject=subject,
                project=project,
                task_id=task.task_id,
                body=body,
            ),
        )
    finally:
        store.close()

    typer.echo(task.task_id)


@alert_app.command("raise")
def alert_raise(
    alert_type: str = typer.Argument(..., help="Alert type."),
    session_name: str = typer.Argument(..., help="Session name from config."),
    message: str = typer.Argument(..., help="Alert message."),
    severity: str = typer.Option("warn", "--severity", help="Alert severity."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    alert = PollyPMService(config_path).raise_alert(alert_type, session_name, message, severity=severity)
    if json_output:
        _emit_json({"alert": alert})
        return
    typer.echo(f"Raised alert #{alert.alert_id} for {session_name}: {alert.alert_type}")


@alert_app.command("clear")
def alert_clear(
    alert_id: int = typer.Argument(..., help="Alert id."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    try:
        alert = PollyPMService(config_path).clear_alert(alert_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        _emit_json({"alert": alert})
        return
    typer.echo(f"Cleared alert #{alert_id}")


@alert_app.command("list")
def alert_list(
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    items = PollyPMService(config_path).list_alerts()
    if json_output:
        _emit_json({"alerts": items})
        return
    if not items:
        typer.echo("No open alerts.")
        return
    for alert in items:
        typer.echo(f"- #{alert.alert_id} {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")


@session_app.command("set-status")
def session_set_status(
    session_name: str = typer.Argument(..., help="Session name from config."),
    status: str = typer.Argument(..., help="Runtime status label."),
    reason: str = typer.Option("", "--reason", help="Optional status reason."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    runtime = PollyPMService(config_path).set_session_status(session_name, status, reason=reason)
    if json_output:
        _emit_json({"session_runtime": runtime})
        return
    typer.echo(f"Updated {session_name} to {status}")


@heartbeat_app.command("install")
def heartbeat_install(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    """Install a cron job that runs the heartbeat sweep every minute."""
    pm_path = shutil.which("pm")
    if pm_path is None:
        raise typer.BadParameter("Cannot find `pm` on PATH.")
    # Include PATH so tmux/claude/codex are findable from cron's minimal env.
    # Also include HOME and SECURITYSESSIONID for macOS Keychain auth
    # (needed for claude CLI Haiku calls).
    import os
    path_dirs = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    home_local = Path.home() / ".local" / "bin"
    if home_local.exists():
        path_dirs = f"{home_local}:{path_dirs}"
    env_parts = [f"PATH={path_dirs}", f"HOME={Path.home()}"]
    session_id = os.environ.get("SECURITYSESSIONID", "")
    if session_id:
        env_parts.append(f"SECURITYSESSIONID={session_id}")
    env_str = " ".join(env_parts)
    cron_line = f"* * * * * {env_str} {pm_path} heartbeat --config {config_path} >> /tmp/pollypm-heartbeat.log 2>&1"
    marker = "# pollypm-heartbeat"
    full_line = f"{cron_line}  {marker}"

    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if marker in existing:
        typer.echo("Heartbeat cron job already installed. Use `pm heartbeat uninstall` to remove it first.")
        return

    new_crontab = existing.rstrip("\n") + "\n" + full_line + "\n" if existing.strip() else full_line + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    typer.echo(f"Installed heartbeat cron job (runs every minute).")
    typer.echo(f"  {cron_line}")
    typer.echo(f"Log: /tmp/pollypm-heartbeat.log")


@heartbeat_app.command("uninstall")
def heartbeat_uninstall() -> None:
    """Remove the heartbeat cron job."""
    marker = "# pollypm-heartbeat"
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode != 0 or marker not in result.stdout:
        typer.echo("No heartbeat cron job found.")
        return

    lines = [line for line in result.stdout.splitlines() if marker not in line]
    new_crontab = "\n".join(lines) + "\n" if lines else ""
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    typer.echo("Removed heartbeat cron job.")


@heartbeat_app.command("record")
def heartbeat_record(
    session_name: str = typer.Argument(..., help="Session name from config."),
    payload_json: str = typer.Argument(..., help="Heartbeat snapshot payload as JSON."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid heartbeat JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("Heartbeat payload must be a JSON object.")
    record = PollyPMService(config_path).record_heartbeat(session_name, payload)
    if json_output:
        _emit_json({"heartbeat": record})
        return
    typer.echo(f"Recorded heartbeat for {session_name}")


@app.command("worker-start")
def worker_start(
    project_key: str = typer.Argument(..., help="Tracked project key."),
    prompt: str | None = typer.Option(None, "--prompt", help="Optional initial worker prompt."),
    role: str = typer.Option(
        "worker",
        "--role",
        help=(
            "Session role. Defaults to 'worker'. Pass e.g. '--role architect' "
            "to spawn a non-worker project-scoped session that the task "
            "sweeper can find via the role-candidate-names resolver."
        ),
    ),
    agent_profile: str | None = typer.Option(
        None,
        "--profile",
        help=(
            "Agent profile to pin on the new session (e.g. 'architect'). "
            "When omitted, the supervisor falls back to the role's default "
            "profile if one exists."
        ),
    ),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    existing = next(
        (
            session
            for session in supervisor.config.sessions.values()
            if session.role == role and session.project == project_key and session.enabled
        ),
        None,
    )
    session = existing or create_worker_session(
        config_path,
        project_key=project_key,
        prompt=prompt,
        role=role,
        agent_profile=agent_profile,
    )
    launch_worker_session(config_path, session.name)
    refreshed = _load_supervisor(config_path)
    launch = next(item for item in refreshed.plan_launches() if item.session.name == session.name)
    label = "Managed worker" if role == "worker" else f"Managed {role}"
    typer.echo(
        f"{label} {session.name} ready for project {project_key} "
        f"in {refreshed.tmux_session_for_launch(launch)}:{launch.window_name}"
    )


@app.command("switch-provider")
def switch_provider(
    session_name: str = typer.Argument(..., help="Session name to switch."),
    provider: str = typer.Argument(..., help="New provider: claude or codex."),
    account: str | None = typer.Option(None, "--account", help="Specific account to use."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    """Switch a worker's provider (e.g., from Codex to Claude) with checkpoint preservation."""
    from pollypm.models import ProviderKind

    supervisor = _load_supervisor(config_path)
    config = supervisor.config

    # Validate the session exists
    session = config.sessions.get(session_name)
    if session is None:
        typer.echo(f"Unknown session: {session_name}")
        raise typer.Exit(code=1)

    # Validate provider
    try:
        new_provider = ProviderKind(provider)
    except ValueError:
        typer.echo(f"Invalid provider: {provider}. Use 'claude' or 'codex'.")
        raise typer.Exit(code=1)

    # Find or auto-select account for the new provider
    if account is None:
        candidates = [
            (name, acct) for name, acct in config.accounts.items()
            if acct.provider == new_provider
        ]
        if not candidates:
            typer.echo(f"No {provider} accounts configured.")
            raise typer.Exit(code=1)
        account = candidates[0][0]

    if account not in config.accounts:
        typer.echo(f"Unknown account: {account}")
        raise typer.Exit(code=1)

    typer.echo(f"Switching {session_name} from {session.provider.value} to {new_provider.value} (account: {account})...")

    # 1. Save checkpoint
    typer.echo("  Saving checkpoint...")
    # The heartbeat already recorded the latest checkpoint

    # 2. Release any leases and stop the old session
    typer.echo("  Stopping old session...")
    try:
        supervisor.release_lease(session_name)
    except Exception:  # noqa: BLE001
        pass
    try:
        supervisor.stop_session(session_name)
    except Exception:  # noqa: BLE001
        pass  # May already be dead

    # 3. Update session config to use the new provider/account + correct args
    typer.echo(f"  Updating config to {new_provider.value}/{account}...")
    from pollypm.onboarding import default_session_args
    new_args = default_session_args(new_provider, open_permissions=config.pollypm.open_permissions_by_default)
    # Update the project-local config if it exists
    project = config.projects.get(session.project)
    if project:
        from pollypm.config import project_config_path
        local_path = project_config_path(project.path)
        if local_path.exists():
            local_content = local_path.read_text()
            import re
            local_content = re.sub(r'provider\s*=\s*"[^"]*"', f'provider = "{new_provider.value}"', local_content)
            local_content = re.sub(r'account\s*=\s*"[^"]*"', f'account = "{account}"', local_content)
            args_str = ", ".join(f'"{a}"' for a in new_args)
            local_content = re.sub(r'args\s*=\s*\[.*?\]', f'args = [{args_str}]', local_content)
            local_path.write_text(local_content)
            typer.echo(f"  Updated project-local config with new args: {new_args}")
    supervisor.store.upsert_session_runtime(
        session_name=session_name,
        status="switching",
        effective_account=account,
        effective_provider=new_provider.value,
    )

    # 4. Relaunch with new provider
    typer.echo("  Relaunching...")
    try:
        supervisor.restart_session(session_name, account, failure_type="provider_switch")
    except Exception as exc:
        typer.echo(f"  Relaunch failed: {exc}")
        raise typer.Exit(code=1)

    typer.echo(f"Switched {session_name} to {new_provider.value}/{account}. Recovery prompt injected.")


@app.command("worker-stop")
def worker_stop(
    session_name: str = typer.Argument(..., help="Worker session name to stop."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    """Stop a worker and mark it disabled so the heartbeat won't recover it."""
    supervisor = _load_supervisor(config_path)
    session = supervisor.config.sessions.get(session_name)
    if session is None:
        typer.echo(f"Unknown session: {session_name}")
        raise typer.Exit(code=1)
    if session.role != "worker":
        typer.echo(f"Can only stop workers, not {session.role} sessions.")
        raise typer.Exit(code=1)

    # Stop the tmux window
    try:
        supervisor.stop_session(session_name)
        typer.echo(f"Stopped {session_name}")
    except Exception:  # noqa: BLE001
        typer.echo(f"Session {session_name} was not running")

    # Mark as disabled so heartbeat won't try to recover it
    supervisor.store.upsert_session_runtime(
        session_name=session_name,
        status="disabled",
    )
    # Clear any open alerts
    for alert_type in ["missing_window", "pane_dead", "recovery_limit", "suspected_loop", "needs_followup"]:
        supervisor.store.clear_alert(session_name, alert_type)
    from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

    supervisor.store.record_event(
        session_name,
        "decommissioned",
        activity_summary(
            summary=f"Worker {session_name} stopped and disabled",
            severity="recommendation",
            verb="decommissioned",
            subject=session_name,
        ),
    )
    typer.echo(f"Marked {session_name} as disabled. Heartbeat will not recover it.")


@app.command()
def repair(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    check_only: bool = typer.Option(False, "--check", help="Report problems without fixing."),
) -> None:
    """Check and repair PollyPM project scaffolding, docs, and state."""
    config_path = _discover_config_path(config_path)
    if not config_path.exists():
        typer.echo(f"Config not found at {config_path}.")
        raise typer.Exit(code=1)
    config = load_config(config_path)
    all_problems: list[str] = []
    all_actions: list[str] = []

    # -- Global docs (in ~/.pollypm itself) --
    global_dir = GLOBAL_CONFIG_DIR
    global_problems = verify_docs(global_dir)
    if global_problems:
        for p in global_problems:
            all_problems.append(f"[global] {p}")
        if not check_only:
            actions = repair_docs(global_dir)
            for a in actions:
                all_actions.append(f"[global] {a}")

    # -- Per-project scaffolding --
    for key, project in config.projects.items():
        project_root = project.path
        if not project_root.exists():
            all_problems.append(f"[{key}] project path does not exist: {project_root}")
            continue

        # Check .pollypm-state scaffold dirs
        state_dir = project_root / ".pollypm-state"
        for subdir in ["dossier", "logs", "artifacts", "checkpoints", "worktrees"]:
            d = state_dir / subdir
            if not d.exists():
                all_problems.append(f"[{key}] missing {d.relative_to(project_root)}")
                if not check_only:
                    d.mkdir(parents=True, exist_ok=True)
                    all_actions.append(f"[{key}] created {d.relative_to(project_root)}")

        # Check instruction dir
        instruction_dir = project_root / ".pollypm"
        for subdir in ["rules", "magic"]:
            d = instruction_dir / subdir
            if not d.exists():
                all_problems.append(f"[{key}] missing .pollypm/{subdir}")
                if not check_only:
                    d.mkdir(parents=True, exist_ok=True)
                    all_actions.append(f"[{key}] created .pollypm/{subdir}")

        # Check docs
        doc_problems = verify_docs(project_root)
        for p in doc_problems:
            all_problems.append(f"[{key}] {p}")
        if not check_only and doc_problems:
            actions = repair_docs(project_root)
            for a in actions:
                all_actions.append(f"[{key}] {a}")

        # Check .gitignore entry
        gitignore = project_root / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            if ".pollypm-state/" not in content:
                all_problems.append(f"[{key}] .gitignore missing .pollypm-state/ entry")
                if not check_only:
                    with gitignore.open("a") as f:
                        if not content.endswith("\n"):
                            f.write("\n")
                        f.write(".pollypm-state/\n")
                    all_actions.append(f"[{key}] added .pollypm-state/ to .gitignore")

    # -- Report --
    if not all_problems:
        typer.echo("All projects healthy. No repairs needed.")
        return
    typer.echo(f"Found {len(all_problems)} problem(s):")
    for p in all_problems:
        typer.echo(f"  - {p}")
    if check_only:
        typer.echo("\nRun `pm repair` (without --check) to fix.")
    else:
        typer.echo(f"\nApplied {len(all_actions)} fix(es):")
        for a in all_actions:
            typer.echo(f"  + {a}")


@app.command()
def upgrade(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    check_only: bool = typer.Option(False, "--check", help="Only check if an upgrade is available."),
) -> None:
    """Check for and install PollyPM updates from GitHub."""
    import importlib.metadata

    try:
        current = importlib.metadata.version("pollypm")
    except importlib.metadata.PackageNotFoundError:
        current = "dev"

    # Check latest version from GitHub
    try:
        result = subprocess.run(
            ["gh", "api", "repos/samhotchkiss/pollypm/releases/latest", "-q", ".tag_name"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # Fallback: check git tags
            result = subprocess.run(
                ["git", "ls-remote", "--tags", "https://github.com/samhotchkiss/pollypm.git"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                typer.echo("Could not check for updates. Are you online?")
                raise typer.Exit(code=1)
            tags = [line.split("refs/tags/")[-1] for line in result.stdout.strip().splitlines() if "refs/tags/" in line]
            tags = [t.lstrip("v") for t in tags if not t.endswith("^{}")]
            if not tags:
                typer.echo(f"Current version: {current}. No releases found on GitHub.")
                return
            latest = sorted(tags)[-1]
        else:
            latest = result.stdout.strip().lstrip("v")
    except FileNotFoundError:
        typer.echo("Neither `gh` nor `git` found. Cannot check for updates.")
        raise typer.Exit(code=1)

    typer.echo(f"Current: {current}")
    typer.echo(f"Latest:  {latest}")

    if current == latest or current == "dev":
        if current == "dev":
            typer.echo("Running from source (dev). Use `git pull` to update.")
        else:
            typer.echo("Already up to date.")
        if not check_only:
            # Still regenerate docs in case templates changed
            typer.echo("\nRegenerating docs from current templates...")
            config = load_config(config_path)
            repair_docs(GLOBAL_CONFIG_DIR)
            for key, project in config.projects.items():
                if project.path.exists():
                    actions = repair_docs(project.path)
                    if actions:
                        typer.echo(f"  [{key}] {len(actions)} doc(s) updated")
            typer.echo("Done.")
        return

    if check_only:
        typer.echo(f"\nUpgrade available: {current} -> {latest}")
        typer.echo("Run `pm upgrade` to install.")
        return

    # Install the update
    typer.echo(f"\nUpgrading {current} -> {latest}...")
    uv = shutil.which("uv")
    pip_cmd: list[str]
    if uv:
        pip_cmd = [uv, "pip", "install", "--upgrade", f"pollypm=={latest}"]
    else:
        pip_cmd = ["pip", "install", "--upgrade", f"pollypm=={latest}"]

    install_result = subprocess.run(pip_cmd, capture_output=True, text=True)
    if install_result.returncode != 0:
        # Try installing from GitHub directly
        typer.echo("PyPI install failed, trying GitHub source...")
        if uv:
            pip_cmd = [uv, "pip", "install", f"git+https://github.com/samhotchkiss/pollypm.git@v{latest}"]
        else:
            pip_cmd = ["pip", "install", f"git+https://github.com/samhotchkiss/pollypm.git@v{latest}"]
        install_result = subprocess.run(pip_cmd, capture_output=True, text=True)
        if install_result.returncode != 0:
            typer.echo(f"Upgrade failed:\n{install_result.stderr}")
            raise typer.Exit(code=1)

    typer.echo("Package updated. Regenerating docs...")
    config = load_config(config_path)
    repair_docs(GLOBAL_CONFIG_DIR)
    for key, project in config.projects.items():
        if project.path.exists():
            actions = repair_docs(project.path)
            if actions:
                typer.echo(f"  [{key}] {len(actions)} doc(s) updated")
    typer.echo(f"Upgrade to {latest} complete. Running sessions are unaffected — restart with `pm reset && pm up` when ready.")
