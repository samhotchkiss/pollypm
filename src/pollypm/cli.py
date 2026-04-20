"""PollyPM CLI root command composition.

Contract:
- Inputs: top-level CLI arguments/options plus delegated feature-module
  registration hooks.
- Outputs: the root ``Typer`` app and a small set of root-owned command
  handlers that compose the feature modules.
- Side effects: loads config, routes through ``PollyPMService``, shells
  out for user-facing commands, and launches TUI surfaces on demand.
- Invariants: feature command families live in ``pollypm.cli_features``;
  this module owns root composition, shared help text, and only the
  remaining cross-cutting root commands.
- Allowed dependencies: service facade, feature registration modules,
  and public CLI/session-service APIs.
- Private: root-only helper functions and compatibility exports relied on
  by existing tests/entry points.
"""

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

# Attach the centralized error log so every ``pm`` invocation writes
# WARNING+ records (plus any tracebacks from logger.exception) into
# ``~/.pollypm/errors.log``. Installed at import time — no plugin
# / rail dependency — so a boot-time crash still lands somewhere
# grep-able. Idempotent.
from pollypm.error_log import install as _install_error_log

_install_error_log(process_label="cli")

from pollypm.accounts import (
    add_account_via_login,
    list_account_statuses,
    probe_account_usage,
    relogin_account,
    remove_account as remove_account_entry,
)
from pollypm.config import (
    DEFAULT_CONFIG_PATH,
    load_config,
    resolve_config_path,
    render_example_config,
    write_example_config,
)
from pollypm.errors import format_config_not_found_error
from pollypm.service_api import PollyPMService
from pollypm.service_api import render_json
from pollypm.cli_features.alerts import alert_app, heartbeat_app, session_app
from pollypm.cli_features.issues import issue_app, itsalive_app, report_app
from pollypm.cli_features.maintenance import register_maintenance_commands
from pollypm.cli_features.projects import register_project_commands
from pollypm.cli_features.ui import register_ui_commands
from pollypm.cli_features.workers import register_worker_commands
from pollypm.session_services import (
    attach_existing_session,
    current_session_name,
    probe_session,
    switch_client_to_session,
)
from pollypm.transcript_ingest import start_transcript_ingestion
from pollypm.workers import create_worker_session, launch_worker_session


# wg05 / #242: every `pm ... --help` gains an Examples section so
# first-time users see copy-paste-ready commands for the common flows
# alongside the raw subcommand table. Bullet formatting survives
# typer's rich re-flow (epilog does not).
_APP_HELP = """PollyPM CLI.

Examples (primary flows):

• pm                                — bring up / attach to the PollyPM session
• pm task next                      — find the next queued task to work on
• pm task claim shortlink_gen/1     — claim a queued task (provisions worktree)
• pm worker-start --role architect <project>  — spawn the project's planner architect
• pm projects                       — list registered projects
• pm help worker                    — full worker onboarding guide

Sub-help:  pm task --help, pm session --help, pm project --help, pm plugins --help.
"""

app = typer.Typer(help=_APP_HELP, invoke_without_command=True, no_args_is_help=False)
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

register_ui_commands(app)
register_project_commands(app)
register_maintenance_commands(app)
register_worker_commands(app)


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

    # Spawn the headless rail daemon so heartbeat + recovery keep
    # ticking even when the cockpit TUI isn't open. Without this,
    # the rail only runs inside the cockpit process — a cockpit
    # crash or a user who just uses the CLI would silently lose
    # auto-recovery, which is exactly how the 2026-04-19 operator
    # outage stayed dead for 5 hours. Idempotent: no-op if a live
    # daemon already holds the PID file.
    _spawn_rail_daemon(config_path)

    # Set up the cockpit layout (split panes) BEFORE the TUI starts,
    # then launch the TUI into the rail pane.
    from pollypm.cockpit_rail import CockpitRouter
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


def _rail_daemon_pid_path() -> Path:
    """Location of the rail-daemon PID file (~/.pollypm/rail_daemon.pid)."""
    return Path(DEFAULT_CONFIG_PATH).parent / "rail_daemon.pid"


def _rail_daemon_live() -> bool:
    """Return True iff the PID file names a currently-running process."""
    import os as _os
    pid_path = _rail_daemon_pid_path()
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    if pid <= 0:
        return False
    try:
        _os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # Stale PID file — clean up for the caller.
        pid_path.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True


def _spawn_rail_daemon(config_path: Path) -> None:
    """Launch ``pollypm.rail_daemon`` detached; best-effort.

    Failures are non-fatal — ``pm up`` succeeds without the daemon,
    users just don't get auto-recovery while the cockpit is closed.
    A warning is printed so the degraded state is visible.

    Tests that exercise ``pm up`` set ``POLLYPM_SKIP_RAIL_DAEMON=1``
    to opt out of the spawn — otherwise they'd leak detached processes
    pointing at their pytest-tmp config paths.
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    if _os.environ.get("POLLYPM_SKIP_RAIL_DAEMON"):
        return
    if _rail_daemon_live():
        return
    pollypm_home = Path(DEFAULT_CONFIG_PATH).parent
    pollypm_home.mkdir(parents=True, exist_ok=True)
    log_path = pollypm_home / "rail_daemon.log"
    try:
        log_fh = open(log_path, "a", buffering=1)  # line-buffered
    except OSError as exc:
        typer.echo(
            f"Warning: could not open rail daemon log {log_path}: {exc}. "
            "Skipping daemon spawn — auto-recovery will only run while "
            "the cockpit is open.",
            err=True,
        )
        return
    try:
        _sp.Popen(
            [_sys.executable, "-m", "pollypm.rail_daemon",
             "--config", str(config_path)],
            stdout=log_fh, stderr=log_fh, stdin=_sp.DEVNULL,
            start_new_session=True,  # detach from tty/process group
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"Warning: rail daemon spawn failed ({exc}). Auto-recovery "
            "will only run while the cockpit is open.",
            err=True,
        )


def _stop_rail_daemon() -> None:
    """Signal the rail daemon to shut down (SIGTERM). Best-effort."""
    import os as _os
    import signal as _signal

    pid_path = _rail_daemon_pid_path()
    if not pid_path.exists():
        return
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        _os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        pass  # already gone
    except Exception:  # noqa: BLE001
        pass
    # Best effort; the daemon's atexit handler removes the file.
    # Clean up here too in case the daemon crashed without signal.
    pid_path.unlink(missing_ok=True)


@app.command("rail-daemon")
def rail_daemon(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    poll_interval: float = typer.Option(60.0, "--poll-interval", help="Seconds between idle-loop wakeups."),
) -> None:
    """Run the headless heartbeat/recovery rail in the foreground.

    This is the same rail ``pm up`` auto-spawns in the background.
    Run it yourself if you want to:
      - supervise it from launchd / systemd
      - watch its log output directly
      - debug scheduler / recovery behavior

    The daemon auto-exits if another rail daemon is already live.
    """
    from pollypm.rail_daemon import run as _run_daemon

    config_path = _discover_config_path(config_path)
    raise typer.Exit(code=_run_daemon(config_path, poll_interval=poll_interval))


@app.command()
def reset(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt."),
) -> None:
    """Kill all PollyPM tmux sessions (cockpit + storage closet). Use `pm up` to restart."""
    config_path = _discover_config_path(config_path)
    if not config_path.exists():
        typer.echo(format_config_not_found_error(config_path), err=True)
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
    _stop_rail_daemon()
    # Clean up all transient state so pm up starts fresh
    jobs_path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
    jobs_path.unlink(missing_ok=True)
    cockpit_state = supervisor.config.project.base_dir / "cockpit_state.json"
    cockpit_state.unlink(missing_ok=True)
    # Clear stale leases — mounted cockpit leases would block recovery on restart
    try:
        from sqlalchemy import delete

        from pollypm.store.schema import messages

        supervisor.store.execute("DELETE FROM leases")
        supervisor.store.execute("DELETE FROM session_runtime")
        supervisor.msg_store.execute(
            delete(messages).where(
                messages.c.type == "alert",
                messages.c.state == "open",
            )
        )
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

    # Resolve priority. 'auto' means "classify by keyword" (see #340 —
    # the classifier moved to :mod:`pollypm.store.classifier`).
    from pollypm.store.classifier import classify_priority, validate_priority

    requested = (priority or "auto").strip().lower()
    if requested == "auto":
        resolved_priority = classify_priority(subject, body)
    else:
        try:
            resolved_priority = validate_priority(requested)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

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
    milestone_key = milestone.strip() or None

    # Route every tier through the unified :class:`Store` messages table.
    # ``tier=silent`` lands with ``state='closed'`` so it stays log-only;
    # ``tier=digest`` inserts at ``state='staged'`` so rollup sweeps can
    # promote; ``tier=immediate`` lands open and shows up in the inbox.
    # The :func:`apply_title_contract` shim inside
    # :meth:`Store.enqueue_message` stamps ``[Action]`` / ``[FYI]`` /
    # ``[Audit]`` prefixes — see :mod:`pollypm.store.title_contract`.
    from pollypm.store import SQLAlchemyStore
    from pollypm.work.cli import _resolve_db_path

    db_path = _resolve_db_path(db, project=project)
    store = SQLAlchemyStore(f"sqlite:///{db_path}")

    # Tier → (state, retained_label) mapping:
    #   immediate → state='open'    (surfaces in inbox immediately)
    #   digest    → state='staged'  (rollup promotes at flush time)
    #   silent    → state='closed'  (audit trail only, never surfaces)
    tier_state = {
        "immediate": "open",
        "digest": "staged",
        "silent": "closed",
    }[resolved_priority]

    payload = {
        "actor": actor,
        "project": project,
        "milestone_key": milestone_key,
        "requester": requester_role,
    }

    try:
        message_id = store.enqueue_message(
            type="notify",
            tier=resolved_priority,
            # recipient routes the row to a reader surface: 'user' →
            # normal inbox, 'polly' → fast-track plan review (see #297).
            recipient=requester_role,
            sender=actor,
            subject=subject,
            body=body,
            scope=project,
            labels=label_list or None,
            payload=payload,
            state=tier_state,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"Failed to enqueue notify message: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        store.close()

    if resolved_priority == "silent":
        typer.echo("silent")
    elif resolved_priority == "digest":
        typer.echo(f"digest:{message_id}")
    else:
        typer.echo(str(message_id))
