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
    alerts = supervisor.run_heartbeat(snapshot_lines=snapshot_lines)
    cli_mod._tick_core_rail_if_available(supervisor)
    if json_output:
        cli_mod._emit_json({"alerts": alerts})
        return
    typer.echo(f"Heartbeat completed. Open alerts: {len(alerts)}")
    for alert in alerts:
        typer.echo(
            f"- {alert.severity} {alert.session_name}/"
            f"{alert.alert_type}#{alert.alert_id}: {alert.message}"
        )


@alert_app.command("raise")
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


@alert_app.command("clear")
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


@alert_app.command("list")
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


@session_app.command("set-status")
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


@heartbeat_app.command("record")
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
