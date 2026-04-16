"""Tests for the morning_briefing plugin — mb01 skeleton + gates.

Covers:

* Plugin structure: capabilities manifest, herald agent profile, roster
  entry on ``@every 1h``, ``briefing.tick`` handler registered.
* Briefing-tick gates: off-hour skip, already-briefed-today skip,
  disabled skip, timezone-override path, missing-config short-circuit.
* Successful fire persists ``last_briefing_date`` — survives reload.
* Herald persona file: non-empty, ≥200 words, matches spec §5 shape.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pollypm.heartbeat.roster import EverySchedule, Roster
from pollypm.jobs import JobHandlerRegistry
from pollypm.plugin_api.v1 import JobHandlerAPI, RosterAPI
from pollypm.plugins_builtin.morning_briefing import plugin as plugin_module
from pollypm.plugins_builtin.morning_briefing.handlers import briefing_tick
from pollypm.plugins_builtin.morning_briefing.handlers.briefing_tick import (
    briefing_tick_handler,
    should_fire,
)
from pollypm.plugins_builtin.morning_briefing.settings import (
    BriefingSettings,
    parse_briefing_settings,
)
from pollypm.plugins_builtin.morning_briefing.state import (
    BriefingState,
    iso_date,
    load_state,
    save_state,
    state_path,
)


UTC = ZoneInfo("UTC")
NY = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Plugin structure
# ---------------------------------------------------------------------------


class TestPluginStructure:
    def test_declared_capabilities(self) -> None:
        caps = {(c.kind, c.name) for c in plugin_module.plugin.capabilities}
        assert ("agent_profile", "herald") in caps
        assert ("job_handler", "briefing.tick") in caps
        assert ("roster_entry", "briefing.tick") in caps

    def test_registers_briefing_tick_handler(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="morning_briefing")
        assert plugin_module.plugin.register_handlers is not None
        plugin_module.plugin.register_handlers(api)
        assert "briefing.tick" in registry.names()

    def test_registers_hourly_roster_entry(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="morning_briefing")
        assert plugin_module.plugin.register_roster is not None
        plugin_module.plugin.register_roster(api)
        entries = {entry.handler_name: entry for entry in roster.entries}
        assert "briefing.tick" in entries
        entry = entries["briefing.tick"]
        assert isinstance(entry.schedule, EverySchedule)
        assert int(entry.schedule.interval.total_seconds()) == 3600

    def test_herald_profile_is_registered(self) -> None:
        assert "herald" in plugin_module.plugin.agent_profiles
        factory = plugin_module.plugin.agent_profiles["herald"]
        profile = factory()
        assert profile.name == "herald"
        assert hasattr(profile, "prompt")
        assert len(profile.prompt) > 0


# ---------------------------------------------------------------------------
# Herald persona file
# ---------------------------------------------------------------------------


class TestHeraldPersona:
    def test_file_exists_and_nonempty(self) -> None:
        path = Path(plugin_module.__file__).parent / "profiles" / "herald.md"
        assert path.exists()
        content = path.read_text()
        assert len(content.strip()) > 0

    def test_persona_is_opinionated(self) -> None:
        content = (
            Path(plugin_module.__file__).parent / "profiles" / "herald.md"
        ).read_text()
        # Spec §5 + mb01 acceptance: ≥200 words, contains required
        # structural markers.
        word_count = len(content.split())
        assert word_count >= 200, f"herald persona has only {word_count} words"
        # Shape markers from spec §5: Yesterday / Today's priorities / Watch.
        assert "Yesterday" in content
        assert "Today's priorities" in content or "Today" in content
        assert "Watch" in content
        # Tone flag from spec — morning-coffee not status-meeting.
        assert "morning" in content.lower()

    def test_preferred_providers_declared(self) -> None:
        content = (
            Path(plugin_module.__file__).parent / "profiles" / "herald.md"
        ).read_text()
        # The spec asks for `preferred_providers: [claude, codex]`.
        assert "claude" in content.lower()
        assert "codex" in content.lower()


# ---------------------------------------------------------------------------
# Settings parsing
# ---------------------------------------------------------------------------


class TestBriefingSettings:
    def test_defaults(self) -> None:
        settings = parse_briefing_settings({})
        assert settings.enabled is True
        assert settings.hour == 6
        assert settings.timezone == ""
        assert settings.priorities_count == 5
        assert settings.quiet_mode_after_days == 7

    def test_enabled_false(self) -> None:
        assert parse_briefing_settings({"enabled": False}).enabled is False

    def test_hour_coercion_and_clamp(self) -> None:
        assert parse_briefing_settings({"hour": 8}).hour == 8
        # Out-of-range falls back to default.
        assert parse_briefing_settings({"hour": 25}).hour == 6
        assert parse_briefing_settings({"hour": -1}).hour == 6
        # Garbage type falls back to default.
        assert parse_briefing_settings({"hour": "banana"}).hour == 6

    def test_timezone_override(self) -> None:
        assert (
            parse_briefing_settings({"timezone": "America/New_York"}).timezone
            == "America/New_York"
        )

    def test_non_dict_input_returns_defaults(self) -> None:
        assert parse_briefing_settings(None) == BriefingSettings()
        assert parse_briefing_settings("oops") == BriefingSettings()


# ---------------------------------------------------------------------------
# should_fire gate logic
# ---------------------------------------------------------------------------


class TestShouldFire:
    def test_disabled_skips(self) -> None:
        settings = BriefingSettings(enabled=False)
        state = BriefingState()
        now = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        fire, reason = should_fire(settings=settings, state=state, now_local=now)
        assert not fire
        assert reason == "disabled"

    def test_off_hour_skips(self) -> None:
        settings = BriefingSettings(hour=6)
        state = BriefingState()
        now = datetime(2026, 4, 16, 9, 0, tzinfo=NY)
        fire, reason = should_fire(settings=settings, state=state, now_local=now)
        assert not fire
        assert reason == "off-hour"

    def test_already_briefed_today_skips(self) -> None:
        settings = BriefingSettings(hour=6)
        state = BriefingState(last_briefing_date="2026-04-16")
        now = datetime(2026, 4, 16, 6, 30, tzinfo=NY)
        fire, reason = should_fire(settings=settings, state=state, now_local=now)
        assert not fire
        assert reason == "already-briefed"

    def test_fires_at_configured_hour_when_fresh(self) -> None:
        settings = BriefingSettings(hour=6)
        state = BriefingState(last_briefing_date="2026-04-15")
        now = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        fire, reason = should_fire(settings=settings, state=state, now_local=now)
        assert fire
        assert reason == "ok"

    def test_custom_hour(self) -> None:
        settings = BriefingSettings(hour=8)
        state = BriefingState()
        # 6 a.m. — wrong hour now.
        fire, _ = should_fire(
            settings=settings, state=state,
            now_local=datetime(2026, 4, 16, 6, 0, tzinfo=NY),
        )
        assert not fire
        # 8 a.m. — right hour.
        fire, _ = should_fire(
            settings=settings, state=state,
            now_local=datetime(2026, 4, 16, 8, 0, tzinfo=NY),
        )
        assert fire


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestBriefingStatePersistence:
    def test_roundtrip(self, tmp_path: Path) -> None:
        state = BriefingState(last_briefing_date="2026-04-16", last_fire_at="2026-04-16T06:00:00-04:00")
        save_state(tmp_path, state)
        reloaded = load_state(tmp_path)
        assert reloaded.last_briefing_date == "2026-04-16"
        assert reloaded.last_fire_at == "2026-04-16T06:00:00-04:00"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_state(tmp_path).last_briefing_date == ""

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        state_path(tmp_path).write_text("{not-json")
        assert load_state(tmp_path).last_briefing_date == ""

    def test_iso_date_helper(self) -> None:
        assert iso_date(datetime(2026, 4, 16).date()) == "2026-04-16"


# ---------------------------------------------------------------------------
# Full handler integration (config on disk, payload-driven now_local)
# ---------------------------------------------------------------------------


def _minimal_config(tmp_path: Path, *, briefing_section: str = "", pollypm_tz: str = "") -> Path:
    """Write a minimal pollypm.toml and return its path."""
    base_dir = tmp_path / "state"
    base_dir.mkdir(parents=True, exist_ok=True)
    tz_line = f'\ntimezone = "{pollypm_tz}"' if pollypm_tz else ""
    config_path = tmp_path / "pollypm.toml"
    config_path.write_text(
        "[project]\n"
        'name = "Fixture"\n'
        f'base_dir = "{base_dir}"\n'
        f'logs_dir = "{base_dir / "logs"}"\n'
        f'snapshots_dir = "{base_dir / "snapshots"}"\n'
        f'state_db = "{base_dir / "state.db"}"\n'
        "\n"
        "[pollypm]\n"
        'controller_account = "acct"'
        + tz_line
        + "\n"
        "\n"
        "[accounts.acct]\n"
        'provider = "claude"\n'
        + briefing_section
    )
    return config_path


class TestTickHandler:
    def test_no_config_short_circuits(self, tmp_path: Path) -> None:
        result = briefing_tick_handler({"config_path": str(tmp_path / "missing.toml")})
        assert result["fired"] is False
        assert result["reason"] == "no-config"

    def test_off_hour_skip(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _minimal_config(tmp_path, pollypm_tz="America/New_York")
        # 9 a.m. NY — not the default 6 a.m. hour.
        now_local = datetime(2026, 4, 16, 9, 0, tzinfo=NY)
        result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is False
        assert result["reason"] == "off-hour"

    def test_already_briefed_today_skip(self, tmp_path: Path) -> None:
        config_path = _minimal_config(tmp_path, pollypm_tz="America/New_York")
        base_dir = tmp_path / "state"
        save_state(base_dir, BriefingState(last_briefing_date="2026-04-16"))

        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is False
        assert result["reason"] == "already-briefed"

    def test_disabled_skip(self, tmp_path: Path) -> None:
        config_path = _minimal_config(
            tmp_path,
            briefing_section='\n[briefing]\nenabled = false\n',
            pollypm_tz="America/New_York",
        )
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is False
        assert result["reason"] == "disabled"

    def test_timezone_override_path(self, tmp_path: Path, monkeypatch) -> None:
        """[briefing].timezone should win over [pollypm].timezone."""
        # pollypm says UTC, briefing says America/New_York. Short-circuit
        # the gather → quiet-mode path since this test only exercises the
        # gate logic — a busy fire_briefing keeps the tick handler on its
        # happy path so we can assert fired=True.
        monkeypatch.setattr(
            briefing_tick, "fire_briefing",
            lambda **kwargs: {"fired": True, "draft": {"mode": "stub"}},
        )
        config_path = _minimal_config(
            tmp_path,
            briefing_section='\n[briefing]\ntimezone = "America/New_York"\nhour = 6\n',
            pollypm_tz="UTC",
        )
        # Supply an explicit now_local *with* NY tz to confirm the handler
        # honours it.
        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        # With default state (never briefed) and NY 6 a.m., we expect a fire.
        assert result["fired"] is True
        assert result["date_local"] == "2026-04-16"

    def test_successful_fire_persists_date_and_survives_reload(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        config_path = _minimal_config(tmp_path, pollypm_tz="America/New_York")
        base_dir = tmp_path / "state"

        calls: list[dict] = []

        def fake_fire(**kwargs):
            calls.append(kwargs)
            return {"fired": True, "stub": False, "marker": "ran"}

        monkeypatch.setattr(briefing_tick, "fire_briefing", fake_fire)

        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is True
        assert result["date_local"] == "2026-04-16"
        assert result["result"]["marker"] == "ran"
        assert len(calls) == 1

        # State persisted to disk.
        reloaded = load_state(base_dir)
        assert reloaded.last_briefing_date == "2026-04-16"
        assert reloaded.last_fire_at  # non-empty

        # Second call within the same local day is a no-op skip.
        again = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert again["fired"] is False
        assert again["reason"] == "already-briefed"
        assert len(calls) == 1  # fire_briefing not called again

    def test_fire_error_does_not_mark_date(self, tmp_path: Path, monkeypatch) -> None:
        """If fire_briefing raises, we must NOT mark the date done."""
        config_path = _minimal_config(tmp_path, pollypm_tz="America/New_York")
        base_dir = tmp_path / "state"

        def boom(**kwargs):
            raise RuntimeError("nope")

        monkeypatch.setattr(briefing_tick, "fire_briefing", boom)

        now_local = datetime(2026, 4, 16, 6, 0, tzinfo=NY)
        result = briefing_tick_handler({
            "config_path": str(config_path),
            "now_local": now_local.isoformat(),
        })
        assert result["fired"] is False
        assert result["reason"] == "fire-error"
        assert load_state(base_dir).last_briefing_date == ""
