"""Developer/test-harness CLI controls."""

from __future__ import annotations

from pathlib import Path

import typer

from pollypm.config import DEFAULT_CONFIG_PATH


dev_app = typer.Typer(
    help=(
        "Developer/test-harness controls for already-running PollyPM "
        "processes."
    )
)


@dev_app.command("simulate-network-dead")
def simulate_network_dead(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    """Make the next PollyPM live-chat dispatch fail as network-dead."""
    from pollypm.dev_network_simulation import arm_network_dead

    marker = arm_network_dead(config_path)
    typer.echo(
        "Armed simulated network-dead for the next PollyPM chat/API dispatch "
        f"(connection refused). Marker: {marker}"
    )


@dev_app.command("simulate-network-clear")
def simulate_network_clear(
    config_path: Path = typer.Option(
        DEFAULT_CONFIG_PATH, "--config", help="PollyPM config path."
    ),
) -> None:
    """Clear a pending simulated network-dead failure."""
    from pollypm.dev_network_simulation import clear_network_dead

    marker = clear_network_dead(config_path)
    typer.echo(f"Cleared simulated network-dead marker: {marker}")

