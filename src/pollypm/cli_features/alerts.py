"""Alert, session, and heartbeat CLI groups.

Contract:
- Inputs: Typer arguments/options for alert and heartbeat operations.
- Outputs: three Typer apps exported as ``alert_app``, ``session_app``,
  and ``heartbeat_app``.
- Side effects: heartbeat ticks, cron installs, and alert/session state
  mutations via ``PollyPMService`` or Supervisor helpers.
- Invariants: grouped operational subcommands stay feature-owned.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import typer

from pollypm.cli_help import help_with_examples
from pollypm.config import DEFAULT_CONFIG_PATH


# Alert taxonomy (#788):
# - "operational" means supervisor-internal noise that should remain in
#   durable logs but should not interrupt the user with a toast.
# - "surfaceable operational" means mechanically detected, but user-
#   actionable. These prefixes must not be folded into operational
#   suppression; they drive rail/project attention instead.
# The cockpit toast filter lives in ``pollypm.cockpit_alerts``; keep this
# CLI-facing note in sync when adding alert types from heartbeat commands.
SURFACEABLE_OPERATIONAL_ALERT_PREFIXES: tuple[str, ...] = (
    "stuck_on_task:",
    "no_session_for_assignment:",
    # The supervisor's recovery loop emits ``recovery_limit`` when it
    # has paused or stopped auto-recovery for a session — a real
    # degradation that needs the user to look. Without this prefix the
    # alert was filtered out of toasts and only visible in Activity /
    # Metrics, which let production-ish failures sit unnoticed (#879).
    "recovery_limit",
)


def is_surfaceable_operational_alert(alert_type: str) -> bool:
    """Return True for operationally detected alerts that still need user visibility."""
    name = alert_type or ""
    return any(name.startswith(prefix) for prefix in SURFACEABLE_OPERATIONAL_ALERT_PREFIXES)


alert_app = typer.Typer(
    help=help_with_examples(
        "Manage durable alerts.",
        [
            ("pm alert list", "show open alerts"),
            ('pm alert raise blocked worker_demo "pane died"', "raise a manual alert"),
            ("pm alert clear 12", "clear one alert by id"),
        ],
    )
)

session_app = typer.Typer(
    help=help_with_examples(
        "Manage session runtime state.",
        [
            ("pm session set-status operator idle", "mark a session idle"),
            (
                'pm session set-status worker_demo working --reason "running tests"',
                "record a working state with context",
            ),
        ],
    )
)

heartbeat_app = typer.Typer(
    help=help_with_examples(
        "Run or record heartbeat state.",
        [
            ("pm heartbeat", "run one heartbeat sweep"),
            ("pm heartbeat --json", "emit the sweep result as JSON"),
            ("pm heartbeat install", "install the cron-based heartbeat runner"),
        ],
    )
)


def _service(config_path: Path):
    from pollypm.service_api import PollyPMService

    return PollyPMService(config_path)


@heartbeat_app.callback(invoke_without_command=True)
def heartbeat(
    ctx: typer.Context,
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    snapshot_lines: int = typer.Option(200, "--snapshot-lines", min=20, help="Lines to capture per pane."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    from pollypm import cli as cli_mod

    supervisor = cli_mod._load_supervisor(config_path)
    # #1546-followup — defense-in-depth rail-daemon liveness check.
    # The cockpit's periodic timer is the primary watcher when it's
    # open; this cron-driven path covers the case where the cockpit
    # itself is dead so nothing else would notice a stale daemon.
    # Best-effort: never blocks the heartbeat sweep.
    try:
        cli_mod._revive_rail_daemon_if_dead(config_path)
    except Exception:  # noqa: BLE001
        pass
    alerts = supervisor.run_heartbeat(snapshot_lines=snapshot_lines)
    tick_result = cli_mod._tick_core_rail_if_available(supervisor)
    cli_mod._drain_and_stop_core_rail_if_available(
        supervisor, tick_result=tick_result,
    )
    if json_output:
        cli_mod._emit_json({"alerts": alerts})
        return
    typer.echo(f"Heartbeat completed. Open alerts: {len(alerts)}")
    for alert in alerts:
        typer.echo(
            f"- {alert.severity} {alert.session_name}/"
            f"{alert.alert_type}#{alert.alert_id}: {alert.message}"
        )


@alert_app.command(
    "raise",
    help=(
        "Manually raise a durable alert against a session. Useful "
        "for surfacing problems that the heartbeat sweep can't "
        "detect on its own."
    ),
)
def alert_raise(
    alert_type: str = typer.Argument(..., help="Alert type."),
    session_name: str = typer.Argument(..., help="Session name from config."),
    message: str = typer.Argument(..., help="Alert message."),
    severity: str = typer.Option("warn", "--severity", help="Alert severity."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    from pollypm import cli as cli_mod

    alert = _service(config_path).raise_alert(
        alert_type,
        session_name,
        message,
        severity=severity,
    )
    if json_output:
        cli_mod._emit_json({"alert": alert})
        return
    typer.echo(f"Raised alert #{alert.alert_id} for {session_name}: {alert.alert_type}")


@alert_app.command("clear", help="Close one alert by id.")
def alert_clear(
    alert_id: int = typer.Argument(..., help="Alert id."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    from pollypm import cli as cli_mod

    try:
        alert = _service(config_path).clear_alert(alert_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        cli_mod._emit_json({"alert": alert})
        return
    typer.echo(f"Cleared alert #{alert_id}")


@alert_app.command(
    "list",
    help="List currently-open alerts (newest first).",
)
def alert_list(
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    from pollypm import cli as cli_mod

    items = _service(config_path).list_alerts()
    if json_output:
        cli_mod._emit_json({"alerts": items})
        return
    if not items:
        typer.echo("No open alerts.")
        return
    for alert in items:
        typer.echo(
            f"- #{alert.alert_id} {alert.severity} "
            f"{alert.session_name}/{alert.alert_type}: {alert.message}"
        )


@session_app.command(
    "set-status",
    help=(
        "Record a session's runtime status (working / idle / "
        "stuck / etc.) with an optional reason."
    ),
)
def session_set_status(
    session_name: str = typer.Argument(..., help="Session name from config."),
    status: str = typer.Argument(..., help="Runtime status label."),
    reason: str = typer.Option("", "--reason", help="Optional status reason."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    from pollypm import cli as cli_mod

    runtime = _service(config_path).set_session_status(
        session_name,
        status,
        reason=reason,
    )
    if json_output:
        cli_mod._emit_json({"session_runtime": runtime})
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
    path_dirs = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    home_local = Path.home() / ".local" / "bin"
    if home_local.exists():
        path_dirs = f"{home_local}:{path_dirs}"
    env_parts = [f"PATH={path_dirs}", f"HOME={Path.home()}"]
    session_id = os.environ.get("SECURITYSESSIONID", "")
    if session_id:
        env_parts.append(f"SECURITYSESSIONID={session_id}")
    env_str = " ".join(env_parts)
    cron_line = (
        f"* * * * * {env_str} {pm_path} heartbeat --config {config_path} "
        ">> /tmp/pollypm-heartbeat.log 2>&1"
    )
    marker = "# pollypm-heartbeat"
    full_line = f"{cron_line}  {marker}"

    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    if marker in existing:
        typer.echo(
            "Heartbeat cron job already installed. "
            "Use `pm heartbeat uninstall` to remove it first."
        )
        return

    new_crontab = (
        existing.rstrip("\n") + "\n" + full_line + "\n"
        if existing.strip()
        else full_line + "\n"
    )
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
    typer.echo("Installed heartbeat cron job (runs every minute).")
    typer.echo(f"  {cron_line}")
    typer.echo("Log: /tmp/pollypm-heartbeat.log")


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


@heartbeat_app.command(
    "install-launchd",
    help=(
        "Install a macOS LaunchAgent that auto-restarts pollypm.rail_daemon. "
        "Layer-3 supervision: launchd watches the rail daemon process, "
        "respawns it if it dies, and starts it on machine boot."
    ),
)
def heartbeat_install_launchd(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
    label: str = typer.Option(
        "com.pollypm.rail-daemon", "--label",
        help="LaunchAgent label (defaults to com.pollypm.rail-daemon).",
    ),
    load: bool = typer.Option(
        True, "--load/--no-load",
        help="Run launchctl bootstrap immediately after installing (default: yes).",
    ),
) -> None:
    """Install ``~/Library/LaunchAgents/<label>.plist`` for the rail daemon."""
    from pollypm.rail_daemon_launchd import install_launchd_keepalive

    try:
        plist_path = install_launchd_keepalive(
            config_path=config_path, label=label, load=load,
        )
    except Exception as exc:  # noqa: BLE001
        raise typer.BadParameter(f"Failed to install LaunchAgent: {exc}") from exc

    typer.echo(f"Installed {plist_path}")
    if load:
        typer.echo(
            f"Loaded {label} via launchctl. The rail daemon will be "
            "auto-restarted if it crashes and on every login."
        )
    else:
        typer.echo(
            f"To activate: launchctl bootstrap gui/$(id -u) {plist_path}"
        )


@heartbeat_app.command(
    "uninstall-launchd",
    help=(
        "Remove the macOS LaunchAgent that supervises pollypm.rail_daemon. "
        "Stops the daemon (if launchd-managed) and unlinks the plist."
    ),
)
def heartbeat_uninstall_launchd(
    label: str = typer.Option(
        "com.pollypm.rail-daemon", "--label",
        help="LaunchAgent label to remove (defaults to com.pollypm.rail-daemon).",
    ),
) -> None:
    """Remove the rail-daemon LaunchAgent and unload it."""
    from pollypm.rail_daemon_launchd import uninstall_launchd_keepalive

    removed = uninstall_launchd_keepalive(label=label)
    if removed:
        typer.echo(f"Removed LaunchAgent {label}.")
    else:
        typer.echo(f"No LaunchAgent named {label} was installed.")


@heartbeat_app.command(
    "status",
    help=(
        "Diagnose the rail daemon's liveness without taking any action. "
        "Mirrors the periodic check that the cockpit / cron path "
        "performs, so operators can see what the supervisor sees."
    ),
)
def heartbeat_status(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Print the rail daemon's diagnosis (alive / missing / dead / stuck)."""
    from pollypm import cli as cli_mod
    from pollypm.config import load_config
    from pollypm.rail_daemon_supervisor import (
        diagnose_rail_daemon, query_last_heartbeat_iso,
    )

    cfg = load_config(config_path)
    pid_path = cli_mod._rail_daemon_pid_path()
    last_tick_iso = query_last_heartbeat_iso(cfg.project.state_db)
    decision = diagnose_rail_daemon(
        pid_path=pid_path, last_tick_iso=last_tick_iso,
    )
    if json_output:
        cli_mod._emit_json({
            "state": decision.state,
            "pid": decision.pid,
            "last_tick_age_seconds": decision.last_tick_age_seconds,
            "reason": decision.reason,
            "needs_revival": decision.needs_revival,
        })
        return
    label = "alive" if decision.state == "alive" else "DEGRADED"
    typer.echo(f"rail_daemon: {label} ({decision.state})")
    typer.echo(f"  pid: {decision.pid}")
    if decision.last_tick_age_seconds is not None:
        typer.echo(f"  last tick: {decision.last_tick_age_seconds:.0f}s ago")
    typer.echo(f"  {decision.reason}")


@heartbeat_app.command(
    "record",
    help=(
        "Record a heartbeat snapshot for a session — used by the "
        "session manager + tests to inject pane state."
    ),
)
def heartbeat_record(
    session_name: str = typer.Argument(..., help="Session name from config."),
    payload_json: str = typer.Argument(..., help="Heartbeat snapshot payload as JSON."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
) -> None:
    from pollypm import cli as cli_mod

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid heartbeat JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("Heartbeat payload must be a JSON object.")
    record = _service(config_path).record_heartbeat(session_name, payload)
    if json_output:
        cli_mod._emit_json({"heartbeat": record})
        return
    typer.echo(f"Recorded heartbeat for {session_name}")
