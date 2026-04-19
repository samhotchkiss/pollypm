"""Worker/session management CLI commands.

Contract:
- Inputs: Typer arguments/options for managed worker lifecycle actions.
- Outputs: root command registrations on the passed Typer app.
- Side effects: tmux/session mutations, config updates, and runtime
  state changes via Supervisor helpers.
- Invariants: worker-management behavior is isolated from unrelated CLI
  concerns.
"""

from __future__ import annotations

from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH


def register_worker_commands(app: typer.Typer) -> None:
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
        from pollypm import cli as cli_mod

        supervisor = cli_mod._load_supervisor(config_path)
        cli_mod._require_pollypm_session(supervisor)
        existing = next(
            (
                session
                for session in supervisor.config.sessions.values()
                if session.role == role and session.project == project_key and session.enabled
            ),
            None,
        )
        session = existing or cli_mod.create_worker_session(
            config_path,
            project_key=project_key,
            prompt=prompt,
            role=role,
            agent_profile=agent_profile,
        )
        cli_mod.launch_worker_session(config_path, session.name)
        refreshed = cli_mod._load_supervisor(config_path)
        launch = next(
            item for item in refreshed.plan_launches()
            if item.session.name == session.name
        )
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
        from pollypm import cli as cli_mod
        from pollypm.models import ProviderKind
        from pollypm.onboarding import default_session_args

        supervisor = cli_mod._load_supervisor(config_path)
        config = supervisor.config

        session = config.sessions.get(session_name)
        if session is None:
            typer.echo(f"Unknown session: {session_name}")
            raise typer.Exit(code=1)

        try:
            new_provider = ProviderKind(provider)
        except ValueError:
            typer.echo(f"Invalid provider: {provider}. Use 'claude' or 'codex'.")
            raise typer.Exit(code=1)

        if account is None:
            candidates = [
                (name, acct)
                for name, acct in config.accounts.items()
                if acct.provider == new_provider
            ]
            if not candidates:
                typer.echo(f"No {provider} accounts configured.")
                raise typer.Exit(code=1)
            account = candidates[0][0]

        if account not in config.accounts:
            typer.echo(f"Unknown account: {account}")
            raise typer.Exit(code=1)

        typer.echo(
            f"Switching {session_name} from {session.provider.value} "
            f"to {new_provider.value} (account: {account})..."
        )
        typer.echo("  Saving checkpoint...")
        typer.echo("  Stopping old session...")
        try:
            supervisor.release_lease(session_name)
        except Exception:  # noqa: BLE001
            pass
        try:
            supervisor.stop_session(session_name)
        except Exception:  # noqa: BLE001
            pass

        typer.echo(f"  Updating config to {new_provider.value}/{account}...")
        new_args = default_session_args(
            new_provider,
            open_permissions=config.pollypm.open_permissions_by_default,
        )
        project = config.projects.get(session.project)
        if project:
            from pollypm.config import project_config_path

            local_path = project_config_path(project.path)
            if local_path.exists():
                local_content = local_path.read_text()
                import re

                local_content = re.sub(
                    r'provider\s*=\s*"[^"]*"',
                    f'provider = "{new_provider.value}"',
                    local_content,
                )
                local_content = re.sub(
                    r'account\s*=\s*"[^"]*"',
                    f'account = "{account}"',
                    local_content,
                )
                args_str = ", ".join(f'"{arg}"' for arg in new_args)
                local_content = re.sub(
                    r'args\s*=\s*\[.*?\]',
                    f'args = [{args_str}]',
                    local_content,
                )
                local_path.write_text(local_content)
                typer.echo(f"  Updated project-local config with new args: {new_args}")
        supervisor.store.upsert_session_runtime(
            session_name=session_name,
            status="switching",
            effective_account=account,
            effective_provider=new_provider.value,
        )

        typer.echo("  Relaunching...")
        try:
            supervisor.restart_session(
                session_name,
                account,
                failure_type="provider_switch",
            )
        except Exception as exc:
            typer.echo(f"  Relaunch failed: {exc}")
            raise typer.Exit(code=1)

        typer.echo(
            f"Switched {session_name} to {new_provider.value}/{account}. "
            "Recovery prompt injected."
        )

    @app.command("worker-stop")
    def worker_stop(
        session_name: str = typer.Argument(..., help="Worker session name to stop."),
        config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."),
    ) -> None:
        """Stop a worker and mark it disabled so the heartbeat won't recover it."""
        from pollypm import cli as cli_mod
        from pollypm.plugins_builtin.activity_feed.summaries import activity_summary

        supervisor = cli_mod._load_supervisor(config_path)
        session = supervisor.config.sessions.get(session_name)
        if session is None:
            typer.echo(f"Unknown session: {session_name}")
            raise typer.Exit(code=1)
        if session.role != "worker":
            typer.echo(f"Can only stop workers, not {session.role} sessions.")
            raise typer.Exit(code=1)

        try:
            supervisor.stop_session(session_name)
            typer.echo(f"Stopped {session_name}")
        except Exception:  # noqa: BLE001
            typer.echo(f"Session {session_name} was not running")

        supervisor.store.upsert_session_runtime(
            session_name=session_name,
            status="disabled",
        )
        for alert_type in [
            "missing_window",
            "pane_dead",
            "recovery_limit",
            "suspected_loop",
            "needs_followup",
        ]:
            supervisor.msg_store.clear_alert(session_name, alert_type)
        supervisor.msg_store.append_event(
            scope=session_name,
            sender=session_name,
            subject="decommissioned",
            payload={
                "message": activity_summary(
                    summary=f"Worker {session_name} stopped and disabled",
                    severity="recommendation",
                    verb="decommissioned",
                    subject=session_name,
                ),
            },
        )
        typer.echo(f"Marked {session_name} as disabled. Heartbeat will not recover it.")
