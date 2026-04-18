"""Tests for the advisor plugin — ad01 skeleton + tick gates.

Covers:

* Plugin structure: capabilities manifest, advisor agent profile, roster
  entry on the configured cadence (default ``@every 30m``),
  ``advisor.tick`` job handler registered, ``advisor.autoclose`` handler
  registered.
* Tick gates: plugin-disabled skip, project-disabled skip, paused skip,
  no-changes skip, in-progress throttle, successful enqueue path.
* State: ``last_tick_at`` always stamped; ``last_run`` only stamped via
  the explicit ``mark_last_run`` call (so a crashed session doesn't
  swallow the signals it was about to review).
* Persona: markdown file present, ≥300 words, opinionated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pollypm.heartbeat.roster import EverySchedule, Roster
from pollypm.jobs import JobHandlerRegistry
from pollypm.plugin_api.v1 import JobHandlerAPI, RosterAPI
from pollypm.plugins_builtin.advisor import plugin as plugin_module
from pollypm.plugins_builtin.advisor.handlers import advisor_tick as tick_module
from pollypm.plugins_builtin.advisor.handlers.advisor_tick import (
    advisor_tick_handler,
    has_in_progress_advisor_task,
    mark_last_run,
)
from pollypm.plugins_builtin.advisor.settings import (
    AdvisorSettings,
    load_advisor_settings,
    parse_advisor_settings,
)
from pollypm.plugins_builtin.advisor.state import (
    AdvisorState,
    Dismissal,
    ProjectAdvisorState,
    is_paused,
    iso_utc_now,
    load_state,
    record_dismissal,
    save_state,
    state_path,
)


# ---------------------------------------------------------------------------
# Plugin structure
# ---------------------------------------------------------------------------


class TestPluginStructure:
    def test_declared_capabilities(self) -> None:
        caps = {(c.kind, c.name) for c in plugin_module.plugin.capabilities}
        assert ("agent_profile", "advisor") in caps
        assert ("job_handler", "advisor.tick") in caps
        assert ("job_handler", "advisor.autoclose") in caps
        assert ("roster_entry", "advisor.tick") in caps
        assert ("roster_entry", "advisor.autoclose") in caps

    def test_registers_tick_handler(self) -> None:
        registry = JobHandlerRegistry()
        api = JobHandlerAPI(registry, plugin_name="advisor")
        assert plugin_module.plugin.register_handlers is not None
        plugin_module.plugin.register_handlers(api)
        assert "advisor.tick" in registry.names()
        assert "advisor.autoclose" in registry.names()

    def test_registers_roster_entry_every_30m(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="advisor")
        assert plugin_module.plugin.register_roster is not None
        plugin_module.plugin.register_roster(api)
        entries = {e.handler_name: e for e in roster.entries}
        assert "advisor.tick" in entries
        tick_entry = entries["advisor.tick"]
        assert isinstance(tick_entry.schedule, EverySchedule)
        assert int(tick_entry.schedule.interval.total_seconds()) == 1800
        assert "advisor.autoclose" in entries
        autoclose_entry = entries["advisor.autoclose"]
        assert int(autoclose_entry.schedule.interval.total_seconds()) == 12 * 3600

    def test_dedupe_key_prevents_double_enqueue(self) -> None:
        roster = Roster()
        api = RosterAPI(roster, plugin_name="advisor")
        plugin_module.plugin.register_roster(api)
        # Second registration with the same dedupe key must be a no-op.
        plugin_module.plugin.register_roster(api)
        ticks = [e for e in roster.entries if e.handler_name == "advisor.tick"]
        assert len(ticks) == 1

    def test_advisor_profile_is_registered(self) -> None:
        assert "advisor" in plugin_module.plugin.agent_profiles
        profile = plugin_module.plugin.agent_profiles["advisor"]()
        assert profile.name == "advisor"

    def test_advisor_profile_builds_non_empty_prompt(self) -> None:
        profile = plugin_module.plugin.agent_profiles["advisor"]()
        prompt = profile.build_prompt(context=None)
        assert prompt is not None
        assert len(prompt.split()) >= 300
        lowered = prompt.lower()
        # Core persona invariants from spec §5.
        assert "silent" in lowered
        assert "advisor" in lowered or "architect" in lowered
        # Structured-JSON output contract.
        assert '"emit"' in prompt
        assert '"rationale_if_silent"' in prompt
        # Preferred providers.
        assert "claude" in lowered


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_parse_defaults(self) -> None:
        s = parse_advisor_settings(None)
        assert s.enabled is True
        assert s.cadence == "@every 30m"

    def test_parse_custom_cadence(self) -> None:
        s = parse_advisor_settings({"enabled": True, "cadence": "@every 2h"})
        assert s.cadence == "@every 2h"

    def test_parse_disabled(self) -> None:
        s = parse_advisor_settings({"enabled": False})
        assert s.enabled is False

    def test_parse_invalid_types_fall_back(self) -> None:
        s = parse_advisor_settings({"enabled": "nope", "cadence": 42})
        assert s.enabled is True
        assert s.cadence == "@every 30m"

    def test_load_settings_missing_file(self, tmp_path: Path) -> None:
        s = load_advisor_settings(tmp_path / "nope.toml")
        assert s == AdvisorSettings()

    def test_load_settings_from_toml(self, tmp_path: Path) -> None:
        cfg = tmp_path / "pollypm.toml"
        cfg.write_text('[advisor]\nenabled = false\ncadence = "@every 1h"\n')
        s = load_advisor_settings(cfg)
        assert s.enabled is False
        assert s.cadence == "@every 1h"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TestState:
    def test_missing_state_is_empty(self, tmp_path: Path) -> None:
        state = load_state(tmp_path)
        assert state.projects == {}

    def test_roundtrip_persists_projects_and_dismissals(self, tmp_path: Path) -> None:
        state = AdvisorState()
        proj = state.get("pollypm")
        proj.last_run = "2026-04-16T10:00:00+00:00"
        proj.recent_dismissals.append(Dismissal(topic="architecture_drift", at="2026-04-15T09:00:00+00:00"))
        save_state(tmp_path, state)
        reloaded = load_state(tmp_path)
        assert "pollypm" in reloaded.projects
        assert reloaded.projects["pollypm"].last_run == "2026-04-16T10:00:00+00:00"
        assert reloaded.projects["pollypm"].recent_dismissals[0].topic == "architecture_drift"

    def test_record_dismissal_caps_at_ten(self, tmp_path: Path) -> None:
        for i in range(15):
            record_dismissal(tmp_path, "demo", f"topic_{i}")
        state = load_state(tmp_path)
        dismissals = state.get("demo").recent_dismissals
        assert len(dismissals) == 10
        # The newest ten should survive; oldest five dropped.
        assert dismissals[-1].topic == "topic_14"
        assert dismissals[0].topic == "topic_5"

    def test_is_paused_future(self) -> None:
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        p = ProjectAdvisorState(pause_until=future)
        assert is_paused(p) is True

    def test_is_paused_past(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        p = ProjectAdvisorState(pause_until=past)
        assert is_paused(p) is False

    def test_is_paused_empty(self) -> None:
        assert is_paused(ProjectAdvisorState()) is False

    def test_is_paused_malformed(self) -> None:
        p = ProjectAdvisorState(pause_until="not-a-date")
        assert is_paused(p) is False

    def test_corrupt_state_returns_empty(self, tmp_path: Path) -> None:
        state_path(tmp_path).write_text("not json at all")
        state = load_state(tmp_path)
        assert state.projects == {}


# ---------------------------------------------------------------------------
# Tick handler — in-memory fixtures
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
    name: str = "pollypm"


@dataclass
class FakeConfig:
    projects: dict[str, FakeKnownProject] = field(default_factory=dict)
    project: FakeProjectSection = None  # type: ignore[assignment]


@dataclass
class FakeTask:
    labels: list[str]
    work_status: str


class FakeWorkService:
    """Tiny in-memory work-service stub for the throttle test."""

    def __init__(self) -> None:
        self.tasks: list[tuple[str, FakeTask]] = []
        self.created: list[dict[str, Any]] = []

    def add(self, project: str, labels: list[str], work_status: str) -> None:
        self.tasks.append((project, FakeTask(labels=labels, work_status=work_status)))

    def list_tasks(self, *, project: str | None = None, work_status: str | None = None, **kw):
        out = []
        for p, t in self.tasks:
            if project is not None and p != project:
                continue
            if work_status is not None and t.work_status != work_status:
                continue
            out.append(t)
        return out


@pytest.fixture
def advisor_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a minimal pollypm.toml + FakeConfig under ``tmp_path``."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    base_dir = project_root / ".pollypm"
    base_dir.mkdir()

    config_path = tmp_path / "pollypm.toml"
    config_path.write_text('[advisor]\nenabled = true\ncadence = "@every 30m"\n')

    cfg = FakeConfig(
        projects={"proj": FakeKnownProject(key="proj", path=project_root, tracked=True)},
        project=FakeProjectSection(base_dir=base_dir, root_dir=project_root, name="proj"),
    )

    # Patch config loading so the tick handler uses our fake config +
    # points at our temp toml file.
    def _fake_load_config(_path):
        return cfg

    def _fake_resolve(_path):
        return config_path

    monkeypatch.setattr("pollypm.config.load_config", _fake_load_config)
    monkeypatch.setattr("pollypm.config.resolve_config_path", _fake_resolve)

    return {
        "config_path": config_path,
        "cfg": cfg,
        "project_root": project_root,
        "base_dir": base_dir,
    }


class TestTickHandler:
    def test_no_config_short_circuits(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        result = advisor_tick_handler({"config_path": str(missing)})
        assert result["fired"] is False
        assert result["reason"] == "no-config"

    def test_plugin_disabled_short_circuits(
        self, tmp_path: Path, advisor_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        advisor_config["config_path"].write_text("[advisor]\nenabled = false\n")
        result = advisor_tick_handler({"config_path": str(advisor_config["config_path"])})
        assert result["fired"] is False
        assert result["reason"] == "plugin-disabled"

    def test_no_changes_skips(
        self, advisor_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tick_module, "detect_changes", lambda path, since: False)
        result = advisor_tick_handler(
            {"config_path": str(advisor_config["config_path"])}
        )
        assert result["fired"] is True
        assert result["enqueued"] == []
        assert result["results"][0]["reason"] == "no-changes"
        assert result["results"][0]["scheduled"] is False
        # last_tick_at stamped even when no project needs review.
        reloaded = load_state(advisor_config["base_dir"])
        assert reloaded.get("proj").last_tick_at != ""
        # last_run NOT stamped — that's ad02 bookkeeping, on session complete.
        assert reloaded.get("proj").last_run == ""

    def test_changes_enqueue_when_no_in_flight(
        self, advisor_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = FakeWorkService()

        def fake_enqueue(**kw) -> dict[str, Any]:
            ws.created.append(kw)
            return {"enqueued": True, "project": kw["project_key"]}

        monkeypatch.setattr(tick_module, "detect_changes", lambda path, since: True)
        monkeypatch.setattr(tick_module, "enqueue_advisor_review", fake_enqueue)

        result = advisor_tick_handler(
            {
                "config_path": str(advisor_config["config_path"]),
                "work_service": ws,
            }
        )
        assert result["fired"] is True
        assert result["enqueued"] == ["proj"]
        assert len(ws.created) == 1
        assert ws.created[0]["project_key"] == "proj"

    def test_in_progress_advisor_task_blocks_pile_up(
        self, advisor_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = FakeWorkService()
        ws.add("proj", labels=["advisor"], work_status="in_progress")
        called: list[dict[str, Any]] = []

        def fake_enqueue(**kw) -> dict[str, Any]:
            called.append(kw)
            return {"enqueued": True}

        monkeypatch.setattr(tick_module, "detect_changes", lambda path, since: True)
        monkeypatch.setattr(tick_module, "enqueue_advisor_review", fake_enqueue)

        result = advisor_tick_handler(
            {
                "config_path": str(advisor_config["config_path"]),
                "work_service": ws,
            }
        )
        assert result["fired"] is True
        assert result["enqueued"] == []
        assert result["results"][0]["reason"] == "in-progress"
        # enqueue_advisor_review must never have been called.
        assert called == []

    def test_paused_project_skips(
        self, advisor_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = load_state(advisor_config["base_dir"])
        state.get("proj").pause_until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        save_state(advisor_config["base_dir"], state)

        called = []
        monkeypatch.setattr(tick_module, "detect_changes", lambda path, since: (called.append(1), True)[1])
        result = advisor_tick_handler(
            {"config_path": str(advisor_config["config_path"])}
        )
        assert result["fired"] is True
        assert result["enqueued"] == []
        assert result["results"][0]["reason"] == "paused"
        # detect_changes must be short-circuited before it's even consulted.
        assert called == []

    def test_project_level_disable_skips(
        self, advisor_config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = load_state(advisor_config["base_dir"])
        state.get("proj").enabled = False
        save_state(advisor_config["base_dir"], state)

        called = []
        monkeypatch.setattr(tick_module, "detect_changes", lambda p, s: (called.append(1), True)[1])
        result = advisor_tick_handler(
            {"config_path": str(advisor_config["config_path"])}
        )
        assert result["results"][0]["reason"] == "project-disabled"
        assert called == []

    def test_ambient_project_picked_up_when_no_tracked_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = tmp_path / "single"
        project_root.mkdir()
        base_dir = project_root / ".pollypm"
        base_dir.mkdir()
        config_path = tmp_path / "pollypm.toml"
        config_path.write_text("[advisor]\nenabled = true\n")

        cfg = FakeConfig(
            projects={},
            project=FakeProjectSection(base_dir=base_dir, root_dir=project_root, name="solo"),
        )
        monkeypatch.setattr("pollypm.config.load_config", lambda _p: cfg)
        monkeypatch.setattr("pollypm.config.resolve_config_path", lambda _p: config_path)

        monkeypatch.setattr(tick_module, "detect_changes", lambda path, since: False)
        result = advisor_tick_handler({"config_path": str(config_path)})
        assert result["fired"] is True
        assert result["tracked"] == ["solo"]

    def test_mark_last_run_updates_state(self, tmp_path: Path) -> None:
        mark_last_run(tmp_path, "proj", at="2026-04-16T12:00:00+00:00")
        state = load_state(tmp_path)
        assert state.get("proj").last_run == "2026-04-16T12:00:00+00:00"


# ---------------------------------------------------------------------------
# has_in_progress_advisor_task — dedicated coverage
# ---------------------------------------------------------------------------


class TestThrottle:
    def test_no_service_returns_false(self) -> None:
        assert has_in_progress_advisor_task(project_key="x", work_service=None) is False

    def test_ignores_non_advisor_labeled_tasks(self) -> None:
        ws = FakeWorkService()
        ws.add("p", labels=["feature"], work_status="in_progress")
        assert has_in_progress_advisor_task(project_key="p", work_service=ws) is False

    def test_finds_queued_advisor_task(self) -> None:
        ws = FakeWorkService()
        ws.add("p", labels=["advisor"], work_status="queued")
        assert has_in_progress_advisor_task(project_key="p", work_service=ws) is True

    def test_finds_review_advisor_task(self) -> None:
        ws = FakeWorkService()
        ws.add("p", labels=["advisor"], work_status="review")
        assert has_in_progress_advisor_task(project_key="p", work_service=ws) is True
