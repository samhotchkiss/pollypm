"""Tests for ad06 `pm advisor` CLI commands + [advisor] config.

Covers:

* pm advisor enable / disable — round-trips [advisor].enabled in
  pollypm.toml, idempotent.
* pm advisor pause --hours / --until — writes pause_until to
  advisor-state.json; the tick respects the marker.
* pm advisor resume — clears pause_until.
* pm advisor status — reports enabled / paused / emits_24h / next_tick.
* Config round-trip: parse_advisor_settings reads the new values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pollypm.plugins_builtin.advisor.cli.advisor_cli import (
    _parse_pause_until,
    _set_advisor_enabled,
    advisor_app,
)
from pollypm.plugins_builtin.advisor.handlers import advisor_tick as tick_module
from pollypm.plugins_builtin.advisor.handlers.advisor_tick import (
    advisor_tick_handler,
)
from pollypm.plugins_builtin.advisor.handlers.history_log import (
    HistoryEntry,
    append_log_entry,
)
from pollypm.plugins_builtin.advisor.settings import (
    load_advisor_settings,
    parse_advisor_settings,
)
from pollypm.plugins_builtin.advisor.state import (
    AdvisorState,
    load_state,
    save_state,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake config env — mirrors the ad04 cli fixture.
# ---------------------------------------------------------------------------


@dataclass
class FakeKnownProject:
    key: str
    path: Path
    tracked: bool = True


@dataclass
class FakeProjectSection:
    base_dir: Path
    root_dir: Path
    name: str = "proj"


@dataclass
class FakeConfig:
    project: FakeProjectSection
    projects: dict = field(default_factory=dict)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    base_dir = tmp_path / ".pollypm-state"
    base_dir.mkdir()
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text("[advisor]\nenabled = true\n")
    cfg = FakeConfig(
        projects={"proj": FakeKnownProject(key="proj", path=project_root)},
        project=FakeProjectSection(base_dir=base_dir, root_dir=project_root, name="proj"),
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.advisor.cli.advisor_cli.load_config",
        lambda _p: cfg,
    )
    monkeypatch.setattr(
        "pollypm.plugins_builtin.advisor.cli.advisor_cli.resolve_config_path",
        lambda _p: config_path,
    )
    # Tick-handler tests use the same config loader — patch that too.
    monkeypatch.setattr("pollypm.config.load_config", lambda _p: cfg)
    monkeypatch.setattr("pollypm.config.resolve_config_path", lambda _p: config_path)
    return {"base_dir": base_dir, "config_path": config_path, "cfg": cfg, "root": project_root}


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


class TestEnableDisable:
    def test_enable_from_disabled_to_enabled(self, env) -> None:
        env["config_path"].write_text("[advisor]\nenabled = false\n")
        result = runner.invoke(advisor_app, ["enable", "--config", str(env["config_path"])])
        assert result.exit_code == 0
        assert "enabled" in result.output
        text = env["config_path"].read_text()
        assert "enabled = true" in text

    def test_disable_from_enabled_to_disabled(self, env) -> None:
        result = runner.invoke(advisor_app, ["disable", "--config", str(env["config_path"])])
        assert result.exit_code == 0
        text = env["config_path"].read_text()
        assert "enabled = false" in text

    def test_enable_idempotent(self, env) -> None:
        result = runner.invoke(advisor_app, ["enable", "--config", str(env["config_path"])])
        assert result.exit_code == 0
        assert "already enabled" in result.output

    def test_adds_section_when_missing(self, env) -> None:
        env["config_path"].write_text("# no advisor section\n")
        _set_advisor_enabled(env["config_path"], True)
        text = env["config_path"].read_text()
        assert "[advisor]" in text
        assert "enabled = true" in text

    def test_json_output(self, env) -> None:
        result = runner.invoke(
            advisor_app, ["disable", "--config", str(env["config_path"]), "--json"],
        )
        data = json.loads(result.output)
        assert data["enabled"] is False
        assert data["changed"] is True


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


class TestParsePauseUntil:
    def test_default_is_24h(self) -> None:
        dt = _parse_pause_until(hours=None, until=None)
        now = datetime.now(UTC)
        assert (dt - now).total_seconds() == pytest.approx(24 * 3600, rel=0.05)

    def test_hours_override(self) -> None:
        dt = _parse_pause_until(hours=6, until=None)
        now = datetime.now(UTC)
        assert (dt - now).total_seconds() == pytest.approx(6 * 3600, rel=0.05)

    def test_until_yyyy_mm_dd(self) -> None:
        dt = _parse_pause_until(hours=None, until="2026-05-01")
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 1

    def test_until_iso(self) -> None:
        dt = _parse_pause_until(hours=None, until="2026-05-01T12:00:00+00:00")
        assert dt == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


class TestPauseResume:
    def test_pause_writes_marker(self, env) -> None:
        result = runner.invoke(
            advisor_app,
            ["pause", "--hours", "4", "--project", "proj",
             "--config", str(env["config_path"]), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["project"] == "proj"
        assert payload["pause_until"]
        state = load_state(env["base_dir"])
        assert state.get("proj").pause_until == payload["pause_until"]

    def test_pause_until_explicit_date(self, env) -> None:
        result = runner.invoke(
            advisor_app,
            ["pause", "--until", "2099-01-01", "--project", "proj",
             "--config", str(env["config_path"]), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "2099" in payload["pause_until"]

    def test_resume_clears_marker(self, env) -> None:
        # Pre-seed a pause marker.
        state = load_state(env["base_dir"])
        state.get("proj").pause_until = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
        save_state(env["base_dir"], state)

        result = runner.invoke(
            advisor_app,
            ["resume", "--project", "proj",
             "--config", str(env["config_path"]), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["was_paused"] is True
        assert load_state(env["base_dir"]).get("proj").pause_until == ""

    def test_resume_no_op_when_not_paused(self, env) -> None:
        result = runner.invoke(
            advisor_app,
            ["resume", "--project", "proj", "--config", str(env["config_path"])],
        )
        assert result.exit_code == 0
        assert "was not paused" in result.output

    def test_pause_marker_honored_by_tick(self, env, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pause.
        runner.invoke(
            advisor_app,
            ["pause", "--hours", "4", "--project", "proj",
             "--config", str(env["config_path"])],
        )
        # detect_changes says "yes lots to review" — but the tick must still skip.
        called = {"n": 0}
        monkeypatch.setattr(tick_module, "detect_changes", lambda *a, **kw: (called.update({"n": called['n'] + 1}), True)[1])
        monkeypatch.setattr(
            tick_module, "enqueue_advisor_review",
            lambda **kw: {"enqueued": True},
        )

        result = advisor_tick_handler({"config_path": str(env["config_path"])})
        reasons = {r["reason"] for r in result["results"]}
        assert "paused" in reasons
        # Paused short-circuits before detect_changes is consulted.
        assert called["n"] == 0

    def test_pause_then_resume_clears_gate(self, env, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pause then resume.
        runner.invoke(
            advisor_app,
            ["pause", "--project", "proj", "--config", str(env["config_path"])],
        )
        runner.invoke(
            advisor_app,
            ["resume", "--project", "proj", "--config", str(env["config_path"])],
        )
        monkeypatch.setattr(tick_module, "detect_changes", lambda *a, **kw: True)
        monkeypatch.setattr(
            tick_module, "enqueue_advisor_review",
            lambda **kw: {"enqueued": True},
        )
        result = advisor_tick_handler({"config_path": str(env["config_path"])})
        assert "proj" in result["enqueued"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_fresh(self, env) -> None:
        result = runner.invoke(
            advisor_app,
            ["status", "--project", "proj",
             "--config", str(env["config_path"]), "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["project"] == "proj"
        assert payload["plugin_enabled"] is True
        assert payload["paused"] is False
        assert payload["last_run"] is None
        assert payload["emits_24h"] == 0

    def test_status_reflects_pause(self, env) -> None:
        state = load_state(env["base_dir"])
        state.get("proj").pause_until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        save_state(env["base_dir"], state)
        result = runner.invoke(
            advisor_app,
            ["status", "--project", "proj",
             "--config", str(env["config_path"]), "--json"],
        )
        payload = json.loads(result.output)
        assert payload["paused"] is True

    def test_status_counts_recent_emits(self, env) -> None:
        # Append two emits inside the last 24h + one older.
        now = datetime.now(UTC)
        for i in range(2):
            append_log_entry(
                env["base_dir"],
                HistoryEntry(
                    timestamp=now.isoformat(), project="proj",
                    decision="emit", topic="architecture_drift",
                    severity="recommendation", summary=f"s{i}",
                ),
            )
        append_log_entry(
            env["base_dir"],
            HistoryEntry(
                timestamp=(now - timedelta(days=3)).isoformat(),
                project="proj", decision="emit", topic="other",
                severity="suggestion", summary="old",
            ),
        )
        result = runner.invoke(
            advisor_app,
            ["status", "--project", "proj",
             "--config", str(env["config_path"]), "--json"],
        )
        payload = json.loads(result.output)
        assert payload["emits_24h"] == 2

    def test_status_plain_text(self, env) -> None:
        result = runner.invoke(
            advisor_app,
            ["status", "--project", "proj", "--config", str(env["config_path"])],
        )
        assert "plugin enabled:" in result.output
        assert "cadence:" in result.output
        assert "emits in last 24h:" in result.output


# ---------------------------------------------------------------------------
# [advisor] config — parse + load.
# ---------------------------------------------------------------------------


class TestAdvisorConfig:
    def test_parse_defaults(self) -> None:
        s = parse_advisor_settings({})
        assert s.enabled is True
        assert s.cadence == "@every 30m"

    def test_parse_custom_cadence(self) -> None:
        s = parse_advisor_settings({"cadence": "@every 2h"})
        assert s.cadence == "@every 2h"

    def test_load_from_toml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "pollypm.toml"
        cfg.write_text('[advisor]\nenabled = false\ncadence = "@every 1h"\n')
        s = load_advisor_settings(cfg)
        assert s.enabled is False
        assert s.cadence == "@every 1h"

    def test_disable_via_cli_stops_all_ticks(self, env, monkeypatch: pytest.MonkeyPatch) -> None:
        """Acceptance §9: pm advisor disable stops all ticks until re-enable."""
        runner.invoke(advisor_app, ["disable", "--config", str(env["config_path"])])
        monkeypatch.setattr(tick_module, "detect_changes", lambda *a, **kw: True)

        result = advisor_tick_handler({"config_path": str(env["config_path"])})
        assert result["fired"] is False
        assert result["reason"] == "plugin-disabled"
