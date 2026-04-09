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
    load_config,
    render_example_config,
    write_example_config,
)
from pollypm.messaging import close_message, create_message, list_open_messages
from pollypm.models import ProviderKind
from pollypm.onboarding import run_onboarding
from pollypm.control_tui import PollyPMApp
from pollypm.service_api import PollyPMService
from pollypm.projects import (
    enable_tracked_project,
    register_project,
    scan_projects as scan_projects_registry,
)
from pollypm.providers import get_provider
from pollypm.supervisor import Supervisor
from pollypm.tmux.client import TmuxClient
from pollypm.worktrees import list_worktrees as list_project_worktrees


app = typer.Typer(help="PollyPM CLI", invoke_without_command=True, no_args_is_help=False)


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
    if current_tmux != expected:
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
    PollyCockpitApp(config_path).run(mouse=True)


@app.command("cockpit-pane")
def cockpit_pane(
    kind: str = typer.Argument(..., help="Pane type: inbox, settings, or project."),
    target: str | None = typer.Argument(None, help="Optional project key for project panes."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    config = load_config(config_path)
    if close:
        archived = close_message(config.project.root_dir, close)
        typer.echo(f"Archived {archived.name}")
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
                controller_account = supervisor.bootstrap_tmux(skip_probe=True)
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

    if current_tmux == session_name:
        supervisor.focus_console()
        typer.echo(f"Already inside tmux session {session_name}")
        return

    if current_tmux:
        raise typer.Exit(code=supervisor.tmux.switch_client(session_name))

    if created:
        supervisor.focus_console()

    raise typer.Exit(code=supervisor.tmux.attach_session(session_name))


@app.command()
def launch(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    up(config_path=config_path)


@app.command()
def status(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    launches, windows, alerts, leases, errors = supervisor.status()

    typer.echo("PollyPM control plane:")
    typer.echo(
        f"- controller_account={_account_label(supervisor, supervisor.config.pollypm.controller_account)} "
        f"failover_enabled={supervisor.config.pollypm.failover_enabled}"
    )
    if supervisor.config.pollypm.failover_accounts:
        typer.echo(
            "- failover_order="
            + ", ".join(_account_label(supervisor, name) for name in supervisor.config.pollypm.failover_accounts)
        )
    else:
        typer.echo("- failover_order=none")

    typer.echo("")
    typer.echo("Configured sessions:")
    for launch in launches:
        account_label = launch.account.email or launch.account.name
        typer.echo(
            f"- {launch.session.name}: role={launch.session.role} provider={launch.session.provider.value} "
            f"account={account_label} project={launch.session.project} "
            f"runtime={launch.account.runtime.value} window={launch.window_name}"
        )

    typer.echo("")
    typer.echo("Project assignments:")
    for project_name, project_launches in supervisor.project_assignments().items():
        labels = ", ".join(
            f"{launch.session.name}->{launch.account.email or launch.account.name}"
            for launch in project_launches
        )
        typer.echo(f"- {project_name}: {labels}")

    typer.echo("")
    typer.echo("Provider availability:")
    seen: set[str] = set()
    for launch in launches:
        provider = get_provider(launch.session.provider, root_dir=supervisor.config.project.root_dir)
        if provider.name in seen:
            continue
        seen.add(provider.name)
        typer.echo(f"- {provider.name}: {'ok' if provider.is_available() else 'missing'}")

    typer.echo("")
    typer.echo("Tmux windows:")
    if windows:
        for window in windows:
            typer.echo(
                f"- {window.session}:{window.index} name={window.name} active={window.active} "
                f"pane={window.pane_id} cmd={window.pane_current_command} dead={window.pane_dead}"
            )
    else:
        typer.echo("- none")

    typer.echo("")
    typer.echo("Open alerts:")
    if alerts:
        for alert in alerts:
            typer.echo(f"- {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")
    else:
        typer.echo("- none")

    typer.echo("")
    typer.echo("Leases:")
    if leases:
        for lease in leases:
            typer.echo(f"- {lease.session_name}: owner={lease.owner} note={lease.note or '-'}")
    else:
        typer.echo("- none")

    typer.echo("")
    typer.echo("Open mail:")
    messages = list_open_messages(supervisor.config.project.root_dir)
    if messages:
        for item in messages:
            typer.echo(f"- {item.path.name}: {item.subject} [{item.sender}]")
    else:
        typer.echo("- none")

    if errors:
        typer.echo("")
        typer.echo("Errors:")
        for error in errors:
            typer.echo(f"- {error}")


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


@app.command()
def heartbeat(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    snapshot_lines: int = typer.Option(200, "--snapshot-lines", min=20, help="Lines to capture per pane."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    alerts = supervisor.run_heartbeat(snapshot_lines=snapshot_lines)
    typer.echo(f"Heartbeat completed. Open alerts: {len(alerts)}")
    for alert in alerts:
        typer.echo(f"- {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")


@app.command()
def alerts(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    items = supervisor.open_alerts()
    if not items:
        typer.echo("No open alerts.")
        return
    for alert in items:
        typer.echo(f"- {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")


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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    supervisor = _load_supervisor(config_path)
    _require_pollypm_session(supervisor)
    supervisor.send_input(session_name, text, owner=owner, force=force, press_enter=not no_enter)
    typer.echo(f"Sent input to {session_name}")
