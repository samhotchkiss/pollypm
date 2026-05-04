from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.cockpit_rail import CockpitRouter
from pollypm.cockpit_rail_routes import LiveSessionRoute
from pollypm.cli import app
from pollypm.dev_network_simulation import (
    SimulatedNetworkDead,
    arm_network_dead,
    clear_network_dead,
    network_dead_armed,
    raise_if_network_dead,
)


def test_network_dead_marker_is_consumed_once(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"

    arm_network_dead(config_path)

    with pytest.raises(SimulatedNetworkDead) as raised:
        raise_if_network_dead(config_path, surface="test dispatch")

    message = str(raised.value)
    assert "network unreachable" in message
    assert "connection refused" in message
    assert not network_dead_armed(config_path)

    # One-shot: the next check should be a no-op unless re-armed.
    raise_if_network_dead(config_path, surface="test dispatch")


def test_network_dead_marker_can_be_cleared(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"

    arm_network_dead(config_path)
    clear_network_dead(config_path)

    assert not network_dead_armed(config_path)


def test_dev_cli_arms_and_clears_network_dead_marker(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    runner = CliRunner()

    armed = runner.invoke(
        app,
        ["dev", "simulate-network-dead", "--config", str(config_path)],
    )

    assert armed.exit_code == 0, armed.output
    assert "connection refused" in armed.output
    assert network_dead_armed(config_path)

    cleared = runner.invoke(
        app,
        ["dev", "simulate-network-clear", "--config", str(config_path)],
    )

    assert cleared.exit_code == 0, cleared.output
    assert not network_dead_armed(config_path)


def test_live_session_route_consumes_network_dead_marker(tmp_path: Path) -> None:
    config_path = tmp_path / "pollypm.toml"
    router = CockpitRouter.__new__(CockpitRouter)
    router.config_path = config_path

    arm_network_dead(config_path)

    with pytest.raises(SimulatedNetworkDead):
        router._route_live_session(
            object(),
            "pollypm:PollyPM",
            LiveSessionRoute(session_name="operator"),
        )

    assert not network_dead_armed(config_path)

