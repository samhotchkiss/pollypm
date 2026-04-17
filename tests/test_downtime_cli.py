"""Tests for dt07 — ``pm downtime`` CLI + [downtime] config block.

Covers every subcommand (add / list / pause / resume / enable / disable
/ status) end-to-end against a fixture pollypm.toml. Verifies:

* ``add`` appends to the user-queue file with the right shape.
* ``list`` shows both queued entries and recent titles.
* ``pause`` writes a pause marker; ``resume`` clears it.
* ``enable`` / ``disable`` mutate [downtime].enabled in the toml.
* ``status`` surfaces the combined state.
* Config reading — ``disabled_categories`` survives round-trip.

Runs Typer's CliRunner so we don't shell out.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.plugins_builtin.downtime.cli import downtime_app
from pollypm.plugins_builtin.downtime.handlers.pick_candidate import (
    USER_QUEUE_RELATIVE_PATH,
    read_user_queue,
)
from pollypm.plugins_builtin.downtime.settings import load_downtime_settings
from pollypm.plugins_builtin.downtime.state import (
    DowntimeState,
    load_state,
    save_state,
)


runner = CliRunner()


def _make_config(tmp_path: Path, *, downtime_section: str = "") -> Path:
    base_dir = tmp_path / "state"
    base_dir.mkdir(parents=True, exist_ok=True)
    root_dir = tmp_path
    logs_dir = base_dir / "logs"
    snapshots_dir = base_dir / "snaps"
    state_db = base_dir / "state.db"
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        'name = "Fixture"\n'
        f'root_dir = "{root_dir}"\n'
        f'base_dir = "{base_dir}"\n'
        f'logs_dir = "{logs_dir}"\n'
        f'snapshots_dir = "{snapshots_dir}"\n'
        f'state_db = "{state_db}"\n'
        "\n"
        "[pollypm]\n"
        'controller_account = "acct"\n'
        "\n"
        "[accounts.acct]\n"
        'provider = "claude"\n'
        + downtime_section
    )
    return config_path


# ---------------------------------------------------------------------------
# add / list
# ---------------------------------------------------------------------------


class TestAddAndList:
    def test_add_appends_queue(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = runner.invoke(
            downtime_app,
            [
                "add", "Try idea X",
                "--kind", "spec_feature",
                "--description", "flesh out X",
                "--priority", "4",
                "--config", str(config),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["queued"] is True
        assert payload["title"] == "Try idea X"
        queued = read_user_queue(tmp_path / USER_QUEUE_RELATIVE_PATH)
        assert len(queued) == 1
        assert queued[0].title == "Try idea X"
        assert queued[0].priority == 4

    def test_add_rejects_unknown_kind(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = runner.invoke(
            downtime_app,
            [
                "add", "bad one",
                "--kind", "nonsense",
                "--config", str(config),
            ],
        )
        assert result.exit_code == 2
        assert "Unknown kind" in result.output

    def test_list_shows_queued_and_recent(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        # Seed queue and state.
        runner.invoke(
            downtime_app,
            ["add", "first",
             "--kind", "audit_docs",
             "--config", str(config)],
        )
        state = load_state(tmp_path / "state")
        state.recent_titles = ["past one", "past two"]
        save_state(tmp_path / "state", state)

        result = runner.invoke(
            downtime_app,
            ["list", "--config", str(config), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["queued_count"] == 1
        assert payload["queued"][0]["title"] == "first"
        assert "past two" in payload["recent_titles"]


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    def test_pause_sets_marker(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = runner.invoke(
            downtime_app,
            ["pause", "--config", str(config), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["paused"] is True
        assert payload["pause_until"]
        # State has it persisted.
        state = load_state(tmp_path / "state")
        assert state.pause_until == payload["pause_until"]

    def test_pause_until_explicit_date(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = runner.invoke(
            downtime_app,
            [
                "pause", "--until", "2099-12-31",
                "--config", str(config),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["pause_until"] == "2099-12-31"

    def test_pause_rejects_garbage_until(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        result = runner.invoke(
            downtime_app,
            ["pause", "--until", "not-a-date", "--config", str(config)],
        )
        assert result.exit_code != 0

    def test_resume_clears_marker(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        save_state(tmp_path / "state", DowntimeState(pause_until="2099-01-01"))
        result = runner.invoke(
            downtime_app,
            ["resume", "--config", str(config), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["resumed"] is True
        assert payload["was_paused"] is True
        state = load_state(tmp_path / "state")
        assert state.pause_until == ""


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_disable_then_enable(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        # Initially no [downtime] section → treat as enabled default.
        settings = load_downtime_settings(config)
        assert settings.enabled is True

        result = runner.invoke(
            downtime_app, ["disable", "--config", str(config), "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["enabled"] is False
        assert payload["changed"] is True
        assert load_downtime_settings(config).enabled is False

        # Re-enable.
        result = runner.invoke(
            downtime_app, ["enable", "--config", str(config), "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["enabled"] is True
        assert payload["changed"] is True
        assert load_downtime_settings(config).enabled is True

    def test_disable_preserves_other_keys(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            downtime_section='\n[downtime]\nenabled = true\nthreshold_pct = 70\ncadence = "@every 6h"\n',
        )
        runner.invoke(downtime_app, ["disable", "--config", str(config)])
        settings = load_downtime_settings(config)
        assert settings.enabled is False
        assert settings.threshold_pct == 70
        assert settings.cadence == "@every 6h"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_json_payload(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            downtime_section='\n[downtime]\nenabled = true\nthreshold_pct = 60\ndisabled_categories = ["build_speculative"]\n',
        )
        runner.invoke(
            downtime_app,
            ["add", "q one", "--kind", "spec_feature", "--config", str(config)],
        )
        result = runner.invoke(
            downtime_app, ["status", "--config", str(config), "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["enabled"] is True
        assert payload["threshold_pct"] == 60
        assert payload["disabled_categories"] == ["build_speculative"]
        assert payload["queued_count"] == 1


# ---------------------------------------------------------------------------
# Config parsing end-to-end
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    def test_defaults_when_no_section(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        settings = load_downtime_settings(config)
        assert settings.enabled is True
        assert settings.threshold_pct == 50
        assert settings.cadence == "@every 12h"
        assert settings.disabled_categories == ()

    def test_disabled_categories_round_trip(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            downtime_section=(
                '\n[downtime]\n'
                'disabled_categories = ["build_speculative", "security_scan"]\n'
            ),
        )
        settings = load_downtime_settings(config)
        assert set(settings.disabled_categories) == {"build_speculative", "security_scan"}
