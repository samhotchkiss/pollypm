from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import typer

from pollypm.account_tui import AccountsApp
from pollypm.accounts import (
    add_account_via_login,
    list_account_statuses,
    probe_account_usage,
    relogin_account,
    remove_account as remove_account_entry,
)
from pollypm.cockpit_ui import PollyCockpitApp, PollyCockpitPaneApp, PollySettingsPaneApp
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    GLOBAL_CONFIG_DIR,
    load_config,
    render_example_config,
    write_example_config,
)
from pollypm.doc_scaffold import repair_docs, scaffold_docs, verify_docs
from pollypm.messaging import close_message, create_message, list_closed_messages, list_open_messages
from pollypm.models import ProviderKind
from pollypm.onboarding import run_onboarding
from pollypm.control_tui import PollyPMApp
from pollypm.service_api import PollyPMService
from pollypm.service_api import render_json
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    scan_projects as scan_projects_registry,
)
from pollypm.providers import get_provider
from pollypm.supervisor import Supervisor
from pollypm.tmux.client import TmuxClient
from pollypm.transcript_ingest import start_transcript_ingestion
from pollypm.workers import create_worker_session, launch_worker_session
from pollypm.worktrees import list_worktrees as list_project_worktrees


app = typer.Typer(help="PollyPM CLI", invoke_without_command=True, no_args_is_help=False)
alert_app = typer.Typer(help="Manage durable alerts.")
session_app = typer.Typer(help="Manage session runtime state.")
heartbeat_app = typer.Typer(help="Run or record heartbeat state.")
issue_app = typer.Typer(help="Manage project issues through the configured backend.")
report_app = typer.Typer(help="Report project status summaries.")
app.add_typer(alert_app, name="alert")
app.add_typer(session_app, name="session")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(issue_app, name="issue")
app.add_typer(report_app, name="report")


def _session_name_candidates() -> list[str]:
    return ["pollypm", "pollypm-storage-closet"]


def _discover_config_path(config_path: Path) -> Path:
    if config_path.exists():
        return config_path
    # If an explicit non-default path was given, respect it as-is
    if config_path != DEFAULT_CONFIG_PATH:
        return config_path
    # The global config lives at ~/.pollypm/pollypm.toml
    return DEFAULT_CONFIG_PATH


def _attach_existing_session_without_config() -> bool:
    tmux = TmuxClient()
    current_tmux = tmux.current_session_name()
    for session_name in _session_name_candidates():
        if not tmux.has_session(session_name):
            continue
        if current_tmux == session_name:
            return True
        if current_tmux:
            raise typer.Exit(code=tmux.switch_client(session_name))
        raise typer.Exit(code=tmux.attach_session(session_name))
    return False


def _load_supervisor(config_path: Path) -> Supervisor:
    return Supervisor(load_config(config_path))


def _account_label(supervisor: Supervisor, account_name: str) -> str:
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


def _require_pollypm_session(supervisor: Supervisor) -> None:
    current_tmux = supervisor.tmux.current_session_name()
    expected = supervisor.config.project.tmux_session
    allowed = {expected, supervisor.storage_closet_session_name()}
    if current_tmux not in allowed:
        raise typer.BadParameter(
            f"This command must run inside tmux session '{expected}'. Use `pm up` to attach first."
        )


def _first_run_setup_and_launch(config_path: Path) -> None:
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


@app.command()
def onboard(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to write the onboarding config."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config file."),
) -> None:
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
def doctor() -> None:
    checks = {
        "tmux": shutil.which("tmux"),
        "claude": shutil.which("claude"),
        "codex": shutil.which("codex"),
        "docker": shutil.which("docker"),
        "inside_tmux": bool(os.environ.get("TMUX")),
    }
    typer.echo(json.dumps(checks, indent=2))


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
    AccountsApp(config_path).run()


@app.command()
def ui(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
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
        PollySettingsPaneApp(config_path).run(mouse=True)
        return
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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    project = register_project(config_path, repo_path, name=name)
    typer.echo(f"Registered project {project.name or project.key} at {project.path}")


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


@app.command("notify")
def notify(
    subject: str = typer.Argument(..., help="Short message subject."),
    body: str = typer.Argument(..., help="Message body."),
    sender: str = typer.Option("pa", "--sender", help="Message sender."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    config = load_config(config_path)
    path = create_message(config.project.root_dir, sender=sender, subject=subject, body=body)
    typer.echo(f"Created message {path.name}")


@app.command("mail")
def mail(
    close: str | None = typer.Option(None, "--close", help="Close a specific open message by filename."),
    archived: bool = typer.Option(False, "--archived", "-a", help="Show archived (closed) messages."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    config = load_config(config_path)
    if close:
        closed = close_message(config.project.root_dir, close)
        typer.echo(f"Archived {closed.name}")
        return
    if archived:
        messages = list_closed_messages(config.project.root_dir)
        if not messages:
            typer.echo("No archived mail.")
            return
        for item in messages:
            typer.echo(f"- {item.path.name}: {item.subject} [{item.sender}] {item.created_at}")
        return
    messages = list_open_messages(config.project.root_dir)
    if not messages:
        typer.echo("No open mail.")
        return
    for item in messages:
        typer.echo(f"- {item.path.name}: {item.subject} [{item.sender}]")


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
                session_name, supervisor._CONSOLE_WINDOW, supervisor._console_command(), remain_on_exit=False,
            )
            supervisor.tmux.set_window_option(f"{session_name}:{supervisor._CONSOLE_WINDOW}", "allow-passthrough", "on")
            supervisor.tmux.set_window_option(f"{session_name}:{supervisor._CONSOLE_WINDOW}", "window-size", "latest")
            supervisor.tmux.set_window_option(f"{session_name}:{supervisor._CONSOLE_WINDOW}", "aggressive-resize", "on")
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
    if json_output:
        _emit_json({"alerts": alerts})
        return
    typer.echo(f"Heartbeat completed. Open alerts: {len(alerts)}")
    for alert in alerts:
        typer.echo(f"- {alert.severity} {alert.session_name}/{alert.alert_type}#{alert.alert_id}: {alert.message}")


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
    windows = supervisor._window_map()
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
    # Include PATH so tmux/claude/codex are findable from cron's minimal env
    path_dirs = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    home_local = Path.home() / ".local" / "bin"
    if home_local.exists():
        path_dirs = f"{home_local}:{path_dirs}"
    cron_line = f"* * * * * PATH={path_dirs} {pm_path} heartbeat --config {config_path} >> /tmp/pollypm-heartbeat.log 2>&1"
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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    existing = next(
        (
            session
            for session in supervisor.config.sessions.values()
            if session.role == "worker" and session.project == project_key and session.enabled
        ),
        None,
    )
    session = existing or create_worker_session(config_path, project_key=project_key, prompt=prompt)
    launch_worker_session(config_path, session.name)
    refreshed = _load_supervisor(config_path)
    launch = next(item for item in refreshed.plan_launches() if item.session.name == session.name)
    typer.echo(
        f"Managed worker {session.name} ready for project {project_key} "
        f"in {refreshed._tmux_session_for_launch(launch)}:{launch.window_name}"
    )


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
                        f.write("\n.pollypm-state/\n")
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
