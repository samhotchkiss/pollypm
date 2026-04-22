"""``pm upgrade`` command registration (#716).

Thin Typer shim over :mod:`pollypm.upgrade`. All logic lives there so
the rail one-click (#719) can invoke the same entry point.
"""

from __future__ import annotations

import typer

from pollypm.cli_help import help_with_examples


def register_upgrade_commands(app: typer.Typer) -> None:
    @app.command(
        help=help_with_examples(
            "Upgrade PollyPM to the latest release on the current channel.",
            [
                ("pm upgrade", "install the latest release on the active channel"),
                ("pm upgrade --check-only", "report what would happen, don't install"),
                ("pm upgrade --channel beta", "one-off beta upgrade (config channel wins otherwise)"),
            ],
        )
    )
    def upgrade(
        channel: str = typer.Option(
            "",
            "--channel",
            help="stable | beta. Empty = read release_channel from config.",
        ),
        check_only: bool = typer.Option(
            False,
            "--check-only",
            help="Show what would happen — run the migration check, fetch target version, exit without installing.",
        ),
        recycle_all: bool = typer.Option(
            False,
            "--recycle-all",
            help="After install, tear down and respawn every live session (Polly, Russell, workers). Use for prompt-critical releases where the in-conversation notice isn't enough.",
        ),
        recycle_idle: bool = typer.Option(
            False,
            "--recycle-idle",
            help="After install, respawn only sessions with no active turn in the last 30 minutes. Active work continues untouched.",
        ),
    ) -> None:
        """Detect the install method and delegate to the right package manager."""
        from pollypm.upgrade import read_changelog_diff, upgrade as run_upgrade

        # ``release_check._resolve_channel`` is the canonical reader for
        # ``config.pollypm.release_channel`` (from #713/#714). Until both
        # land, fall back to stable if the helper isn't importable —
        # never crash on a fresh checkout just because the config field
        # is missing.
        try:
            from pollypm.release_check import _resolve_channel
            default_channel = _resolve_channel(None)
        except ImportError:
            default_channel = "stable"
        resolved_channel = channel or default_channel
        result = run_upgrade(
            channel=resolved_channel,
            check_only=check_only,
            recycle_all=recycle_all,
            recycle_idle=recycle_idle,
            emit=typer.echo,
        )

        typer.echo("")
        typer.echo(f"installer: {result.installer}")
        typer.echo(f"version:   {result.old_version} → {result.new_version}")
        typer.echo(f"migration: {'checked' if result.migration_checked else 'skipped'}")
        typer.echo(f"notified:  {'yes' if result.notified else 'no'}")
        typer.echo(f"status:    {result.message}")

        if not result.ok:
            if result.stderr:
                typer.echo("")
                typer.echo(result.stderr)
            raise typer.Exit(code=1)

        if not check_only and result.old_version != result.new_version:
            diff = read_changelog_diff(result.new_version)
            if diff:
                typer.echo("")
                typer.echo("-- CHANGELOG --")
                typer.echo(diff)
