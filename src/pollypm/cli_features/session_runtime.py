"""Session/runtime/operator CLI commands.

Contract:
- Inputs: Typer options/arguments plus helper callbacks exported by
  ``pollypm.cli`` for supervisor loading, config-path resolution, and
  shared JSON/session guards.
- Outputs: root command registrations on the passed Typer app.
- Side effects: session startup/shutdown, lease mutations, pane sends,
  inbox notifications, and diagnostic reads against the supervisor/store.
- Invariants: session/runtime command bodies stay out of ``pollypm.cli``;
  the root module remains composition plus shared compatibility helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer


def register_session_runtime_commands(app: typer.Typer, *, helpers) -> None:
    @app.command(help=helpers._UP_HELP)
    def launch(
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        helpers.up(config_path=config_path)

    @app.command("rail-daemon")
    def rail_daemon(
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
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

        config_path = helpers._discover_config_path(config_path)
        raise typer.Exit(code=_run_daemon(config_path, poll_interval=poll_interval))

    @app.command()
    def reset(
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
        force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation prompt."),
    ) -> None:
        """Kill all PollyPM tmux sessions (cockpit + storage closet). Use `pm up` to restart."""
        from pollypm.errors import format_config_not_found_error

        config_path = helpers._discover_config_path(config_path)
        if not config_path.exists():
            typer.echo(format_config_not_found_error(config_path), err=True)
            raise typer.Exit(code=1)
        supervisor = helpers._load_supervisor(config_path)
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
        helpers._stop_rail_daemon()
        jobs_path = supervisor.config.project.base_dir / "scheduler" / "jobs.json"
        jobs_path.unlink(missing_ok=True)
        cockpit_state = supervisor.config.project.base_dir / "cockpit_state.json"
        cockpit_state.unlink(missing_ok=True)
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

    @app.command(help=helpers._STATUS_HELP)
    def status(
        session_name: str | None = typer.Argument(None, help="Optional session name from config."),
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        if not helpers._config_option_was_explicit():
            config_path = helpers._discover_config_path(config_path)
        from pollypm.service_api import PollyPMService

        payload = PollyPMService(config_path).session_status(session_name)
        sessions = payload["sessions"]
        if session_name is not None and not sessions:
            raise typer.BadParameter(f"Unknown session: {session_name}")
        if json_output:
            helpers._emit_json(payload)
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
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        for launch in supervisor.plan_launches():
            typer.echo(f"[{launch.session.name}]")
            typer.echo(f"window = {launch.window_name}")
            typer.echo(f"log = {launch.log_path}")
            typer.echo(f"command = {launch.command}")
            typer.echo("")

    @app.command()
    def alerts(
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        from pollypm.service_api import PollyPMService

        items = PollyPMService(config_path).list_alerts()
        if not items:
            typer.echo("No open alerts.")
            return
        if json_output:
            helpers._emit_json({"alerts": items})
            return
        for alert in items:
            typer.echo(f"- #{alert.alert_id} {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")

    @app.command("failover")
    def failover(
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        """Show failover configuration: controller account and failover order."""
        from pollypm.config import load_config

        config = load_config(config_path)
        typer.echo(f"Controller: {config.pollypm.controller_account}")
        typer.echo(f"Failover enabled: {'yes' if config.pollypm.failover_enabled else 'no'}")
        if config.pollypm.failover_accounts:
            typer.echo("Failover order:")
            for index, name in enumerate(config.pollypm.failover_accounts, 1):
                account = config.accounts.get(name)
                label = f"{account.email} [{account.provider.value}]" if account else name
                typer.echo(f"  {index}. {label}")
        else:
            typer.echo("No failover accounts configured.")

    @app.command("debug")
    def debug_command(
        session: str | None = typer.Option(None, "--session", "-s", help="Filter to a specific session."),
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        """Show diagnostic info: open alerts, session states, recent events. Works outside tmux."""
        supervisor = helpers._load_supervisor(config_path)

        all_alerts = supervisor.open_alerts()
        alerts_list = [alert for alert in all_alerts if session is None or alert.session_name == session]
        typer.echo(f"Open alerts: {len(alerts_list)}")
        for alert in alerts_list:
            typer.echo(f"  {alert.severity} {alert.session_name}/{alert.alert_type}: {alert.message}")

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

        typer.echo("")
        events_list = supervisor.store.recent_events(limit=5)
        if session is not None:
            events_list = [event for event in events_list if event.session_name == session]
        typer.echo(f"Recent events: {len(events_list)}")
        for event in events_list[:5]:
            typer.echo(f"  {event.created_at} {event.session_name}/{event.event_type}: {event.message}")

    @app.command()
    def events(
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
        limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum number of events to show."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
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
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        supervisor.claim_lease(session_name, owner, note)
        typer.echo(f"Lease set on {session_name} for {owner}")

    @app.command()
    def release(
        session_name: str = typer.Argument(..., help="Session name from config."),
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
        helpers._require_pollypm_session(supervisor)
        supervisor.release_lease(session_name)
        typer.echo(f"Lease released for {session_name}")

    @app.command(help=helpers._SEND_HELP)
    def send(
        session_name: str = typer.Argument(..., help="Session name from config."),
        text: str = typer.Argument(..., help="Text to send into the tmux pane."),
        owner: str = typer.Option("pollypm", "--owner", help="Sender label for lease checks."),
        force: bool = typer.Option(False, "--force", help="Bypass a conflicting lease."),
        no_enter: bool = typer.Option(False, "--no-enter", help="Do not send Enter after the text."),
        json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
        config_path: Path = typer.Option("~/.pollypm/pollypm.toml", "--config", help="PollyPM config path."),
    ) -> None:
        supervisor = helpers._load_supervisor(config_path)
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
            supervisor.send_input(
                session_name,
                text,
                owner=owner,
                force=force,
                press_enter=not no_enter,
            )
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if json_output:
            helpers._emit_json(
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

    @app.command(help=helpers._NOTIFY_HELP)
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
        """Create a work-service inbox item for the human user."""
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

        requester_role = (requester or "user").strip().lower()
        if requester_role not in ("user", "polly"):
            typer.echo(
                f"Error: --requester must be 'user' or 'polly' (got {requester!r}).",
                err=True,
            )
            raise typer.Exit(code=1)

        label_list = [label for label in (labels or []) if label and label.strip()]
        milestone_key = milestone.strip() or None

        from pollypm.store import SQLAlchemyStore
        from pollypm.work.cli import _resolve_db_path

        db_path = _resolve_db_path(db, project=project)
        store = SQLAlchemyStore(f"sqlite:///{db_path}")
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
