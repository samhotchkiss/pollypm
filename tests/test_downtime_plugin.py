"""Tests for the downtime plugin — dt01 skeleton + gates.

Covers:

* Plugin structure: capabilities, explorer profile, roster entry, job
  handler registration.
* Tick-handler gates: disabled, paused, capacity-too-high, throttled
  (in-progress downtime task present), no-candidates, and the happy
  path where a candidate is scheduled.
* Settings parsing: defaults, enabled toggle, threshold clamp, cadence
  default, disabled_categories filtering.
* State persistence: pause marker round-trip, recent_titles ring bound.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pollypm.heartbeat.roster import EverySchedule, Roster
from pollypm.jobs import JobHandlerRegistry
from pollypm.plugin_api.v1 import JobHandlerAPI, RosterAPI
from pollypm.plugins_builtin.downtime import plugin as plugin_module
from pollypm.plugins_builtin.downtime.handlers import downtime_tick
from pollypm.plugins_builtin.downtime.handlers.downtime_tick import (
    Candidate,
    downtime_tick_handler,
    is_paused,
)
from pollypm.plugins_builtin.downtime.settings import (
    DEFAULT_CADENCE,
    DowntimeSettings,
    parse_downtime_settings,
)
from pollypm.plugins_builtin.downtime.state import (
    RECENT_TITLE_LIMIT,
    DowntimeState,
    load_state,
    save_state,
)


# ---------------------------------------------------------------------------
# Plugin structure
# ---------------------------------------------------------------------------


class TestPluginStructure:
    def test_declared_capabilities(self) -> None:
        caps = {(c.kind, c.name) for c in plugin_module.plugin.capabilities}
        assert ("agent_profile", "explorer") in caps
        assert ("job_handler", "downtime.tick") in caps
        assert ("roster_entry", "downtime.tick") in caps

    def test_registers_tick_handler(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="downtime")
        assert plugin_module.plugin.register_handlers is not None
        plugin_module.plugin.register_handlers(api)
        assert "downtime.tick" in registry.names()

    def test_registers_12h_roster_entry(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="downtime")
        assert plugin_module.plugin.register_roster is not None
        plugin_module.plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        assert "downtime.tick" in entries
        entry = entries["downtime.tick"]
        assert isinstance(entry.schedule, EverySchedule)
        assert int(entry.schedule.interval.total_seconds()) == 12 * 3600

    def test_explorer_profile_registered(self) -> None:
        assert "explorer" in plugin_module.plugin.agent_profiles
        profile = plugin_module.plugin.agent_profiles["explorer"]()
        assert profile.name == "explorer"
        assert profile.prompt  # non-empty


class TestExplorerPersona:
    def test_persona_file_exists_and_opinionated(self) -> None:
        path = Path(plugin_module.__file__).parent / "profiles" / "explorer.md"
        assert path.exists()
        content = path.read_text()
        # spec §2 + dt01 acceptance: opinionated persona.
        assert len(content.split()) >= 150
        # Never-auto-deploy reminders.
        lower = content.lower()
        assert "main" in lower
        assert "branch" in lower
        assert "approval" in lower
        assert "preferred_providers" in content

    def test_persona_declares_preferred_providers(self) -> None:
        content = (
            Path(plugin_module.__file__).parent / "profiles" / "explorer.md"
        ).read_text()
        assert "claude" in content.lower()
        assert "codex" in content.lower()


# ---------------------------------------------------------------------------
# Settings parsing
# ---------------------------------------------------------------------------


class TestDowntimeSettings:
    def test_defaults(self) -> None:
        s = parse_downtime_settings({})
        assert s.enabled is True
        assert s.threshold_pct == 50
        assert s.cadence == DEFAULT_CADENCE
        assert s.disabled_categories == ()

    def test_none_returns_defaults(self) -> None:
        assert parse_downtime_settings(None) == DowntimeSettings()
        assert parse_downtime_settings("oops") == DowntimeSettings()

    def test_enabled_false(self) -> None:
        assert parse_downtime_settings({"enabled": False}).enabled is False

    def test_threshold_clamp(self) -> None:
        assert parse_downtime_settings({"threshold_pct": 80}).threshold_pct == 80
        # Out of range — fall back to default.
        assert parse_downtime_settings({"threshold_pct": -5}).threshold_pct == 50
        assert parse_downtime_settings({"threshold_pct": 101}).threshold_pct == 50
        assert parse_downtime_settings({"threshold_pct": "abc"}).threshold_pct == 50

    def test_cadence_override(self) -> None:
        assert parse_downtime_settings({"cadence": "@every 6h"}).cadence == "@every 6h"
        # Empty falls back.
        assert parse_downtime_settings({"cadence": ""}).cadence == DEFAULT_CADENCE

    def test_disabled_categories_filters_unknown(self) -> None:
        s = parse_downtime_settings(
            {"disabled_categories": ["build_speculative", "bogus", "audit_docs"]}
        )
        assert "build_speculative" in s.disabled_categories
        assert "audit_docs" in s.disabled_categories
        assert "bogus" not in s.disabled_categories


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestDowntimeState:
    def test_roundtrip(self, tmp_path: Path) -> None:
        state = DowntimeState(
            pause_until="2026-04-20",
            last_kind="audit_docs",
            last_source="planner",
            recent_titles=["one", "two"],
        )
        save_state(tmp_path, state)
        reloaded = load_state(tmp_path)
        assert reloaded.pause_until == "2026-04-20"
        assert reloaded.last_kind == "audit_docs"
        assert reloaded.last_source == "planner"
        assert reloaded.recent_titles == ["one", "two"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_state(tmp_path).pause_until == ""

    def test_note_scheduled_bounds_titles(self, tmp_path: Path) -> None:
        state = DowntimeState()
        for i in range(RECENT_TITLE_LIMIT + 10):
            state.note_scheduled(kind="spec_feature", source="planner", title=f"t{i}")
        assert len(state.recent_titles) == RECENT_TITLE_LIMIT
        # Oldest dropped, newest kept.
        assert state.recent_titles[-1] == f"t{RECENT_TITLE_LIMIT + 9}"

    def test_note_scheduled_dedupes_titles(self) -> None:
        state = DowntimeState()
        state.note_scheduled(kind="spec_feature", source="user", title="same")
        state.note_scheduled(kind="spec_feature", source="user", title="same")
        assert state.recent_titles == ["same"]


# ---------------------------------------------------------------------------
# is_paused
# ---------------------------------------------------------------------------


class TestIsPaused:
    def test_no_marker(self) -> None:
        assert is_paused(DowntimeState(), now=datetime(2026, 4, 16, tzinfo=UTC)) is False

    def test_past_date_not_paused(self) -> None:
        state = DowntimeState(pause_until="2026-04-10")
        assert is_paused(state, now=datetime(2026, 4, 16, tzinfo=UTC)) is False

    def test_future_date_paused(self) -> None:
        state = DowntimeState(pause_until="2026-04-20")
        assert is_paused(state, now=datetime(2026, 4, 16, tzinfo=UTC)) is True

    def test_same_day_paused(self) -> None:
        state = DowntimeState(pause_until="2026-04-16")
        # "pause through 2026-04-16" should cover any time on that date.
        assert is_paused(state, now=datetime(2026, 4, 16, 5, 0, tzinfo=UTC)) is True

    def test_iso_datetime(self) -> None:
        state = DowntimeState(pause_until="2026-04-16T18:00:00+00:00")
        assert is_paused(state, now=datetime(2026, 4, 16, 17, 0, tzinfo=UTC)) is True
        assert is_paused(state, now=datetime(2026, 4, 16, 19, 0, tzinfo=UTC)) is False

    def test_garbage_marker_ignored(self) -> None:
        state = DowntimeState(pause_until="not a date")
        assert is_paused(state, now=datetime(2026, 4, 16, tzinfo=UTC)) is False


# ---------------------------------------------------------------------------
# Tick handler integration
# ---------------------------------------------------------------------------


def _minimal_config(tmp_path: Path, *, downtime_section: str = "") -> Path:
    """Write a minimal pollypm.toml and return its path.

    Mirrors the morning_briefing test fixture — small on purpose so
    config load doesn't bring in unrelated failures.
    """
    base_dir = tmp_path / "state"
    base_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = base_dir / "logs"
    snapshots_dir = base_dir / "snapshots"
    state_db = base_dir / "state.db"
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        'name = "Fixture"\n'
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


class TestTickHandler:
    def test_no_config_short_circuits(self, tmp_path: Path) -> None:
        result = downtime_tick_handler({"config_path": str(tmp_path / "missing.toml")})
        assert result["scheduled"] is None
        assert result["skipped"] is True
        assert result["reason"] == "no-config"

    def test_disabled_skip(self, tmp_path: Path) -> None:
        config_path = _minimal_config(
            tmp_path, downtime_section='\n[downtime]\nenabled = false\n'
        )
        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["skipped"] is True
        assert result["reason"] == "disabled"

    def test_paused_skip(self, tmp_path: Path) -> None:
        config_path = _minimal_config(tmp_path)
        # Persist pause marker that's clearly in the future.
        base_dir = tmp_path / "state"
        save_state(base_dir, DowntimeState(pause_until="2099-01-01"))
        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["skipped"] is True
        assert result["reason"] == "paused"

    def test_capacity_too_high_skip(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _minimal_config(
            tmp_path, downtime_section='\n[downtime]\nthreshold_pct = 40\n'
        )
        # Force used_pct >= threshold_pct so the capacity gate trips.
        monkeypatch.setattr(downtime_tick, "compute_used_pct", lambda *a, **kw: 80)
        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["skipped"] is True
        assert result["reason"] == "capacity-too-high"
        assert result["used_pct"] == 80
        assert result["threshold_pct"] == 40

    def test_throttled_skip_when_active_downtime_task(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config_path = _minimal_config(tmp_path)
        # Pretend the work.db exists so has_active_downtime_task is invoked.
        base_dir = tmp_path / "state"
        (base_dir / "work.db").write_text("")  # placeholder
        monkeypatch.setattr(downtime_tick, "compute_used_pct", lambda *a, **kw: 10)
        monkeypatch.setattr(
            downtime_tick, "has_active_downtime_task", lambda **kwargs: True
        )
        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["skipped"] is True
        assert result["reason"] == "throttled"

    def test_no_candidates_skip(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _minimal_config(tmp_path)
        monkeypatch.setattr(downtime_tick, "compute_used_pct", lambda *a, **kw: 10)
        monkeypatch.setattr(
            downtime_tick, "has_active_downtime_task", lambda **kwargs: False
        )
        # pick_candidate default already returns None; be explicit to
        # document the contract.
        monkeypatch.setattr(downtime_tick, "pick_candidate", lambda **kw: None)
        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["skipped"] is True
        assert result["reason"] == "no-candidates"

    def test_successful_schedule(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _minimal_config(tmp_path)
        cand = Candidate(
            title="try idea X",
            kind="spec_feature",
            description="Spec out idea X.",
            source="planner",
            priority=4,
        )
        monkeypatch.setattr(downtime_tick, "compute_used_pct", lambda *a, **kw: 10)
        monkeypatch.setattr(
            downtime_tick, "has_active_downtime_task", lambda **kwargs: False
        )
        monkeypatch.setattr(downtime_tick, "pick_candidate", lambda **kw: cand)
        monkeypatch.setattr(
            downtime_tick, "schedule_downtime_task",
            lambda **kw: "fixture/42",
        )

        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["scheduled"] == "fixture/42"
        assert result["skipped"] is False
        assert result["kind"] == "spec_feature"
        assert result["source"] == "planner"

        # State updated.
        state = load_state(tmp_path / "state")
        assert state.last_kind == "spec_feature"
        assert state.last_source == "planner"
        assert "try idea X" in state.recent_titles

    def test_disabled_category_blocks_scheduling(self, tmp_path: Path, monkeypatch) -> None:
        """A candidate whose kind is in disabled_categories must be refused."""
        config_path = _minimal_config(
            tmp_path,
            downtime_section='\n[downtime]\ndisabled_categories = ["build_speculative"]\n',
        )
        cand = Candidate(
            title="prototype feature",
            kind="build_speculative",
            description="Build feature X speculatively.",
            source="user",
            priority=3,
        )
        monkeypatch.setattr(downtime_tick, "compute_used_pct", lambda *a, **kw: 10)
        monkeypatch.setattr(
            downtime_tick, "has_active_downtime_task", lambda **kwargs: False
        )
        monkeypatch.setattr(downtime_tick, "pick_candidate", lambda **kw: cand)
        monkeypatch.setattr(
            downtime_tick, "schedule_downtime_task",
            lambda **kw: pytest.fail("should not be called"),
        )
        result = downtime_tick_handler({"config_path": str(config_path)})
        assert result["skipped"] is True
        assert result["reason"] == "category-disabled"
        assert result["kind"] == "build_speculative"
