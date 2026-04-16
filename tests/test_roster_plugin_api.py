"""Tests for the RosterAPI plugin registration surface."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from pollypm.heartbeat.roster import (
    CronSchedule,
    EverySchedule,
    OnStartupSchedule,
    Roster,
)
from pollypm.plugin_api.v1 import PollyPMPlugin, RosterAPI


# ---------------------------------------------------------------------------
# RosterAPI semantics
# ---------------------------------------------------------------------------


class TestRosterAPI:
    def test_register_cron(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="sweep_plugin")
        assert api.register_recurring("*/5 * * * *", "sweep", {"p": "a"}) is True

        snap = api.snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert isinstance(entry.schedule, CronSchedule)
        assert entry.handler_name == "sweep"
        assert entry.payload == {"p": "a"}

    def test_register_every_duration(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="hb")
        api.register_recurring("@every 60s", "beat")
        entry = api.snapshot()[0]
        assert isinstance(entry.schedule, EverySchedule)
        assert entry.schedule.interval == timedelta(seconds=60)

    def test_register_on_startup(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="boot")
        api.register_recurring("@on_startup", "init")
        entry = api.snapshot()[0]
        assert isinstance(entry.schedule, OnStartupSchedule)

    def test_register_dedupe_key_passthrough(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="p")
        api.register_recurring(
            "@every 60s", "sweep", {"project": "pollypm"}, dedupe_key="sweep:pollypm"
        )
        entry = api.snapshot()[0]
        assert entry.dedupe_key == "sweep:pollypm"

    def test_duplicate_registration_returns_false_and_fires_callback(self) -> None:
        collisions: list[tuple[str, str, str]] = []

        def on_collision(plugin_name: str, handler_name: str, schedule: str) -> None:
            collisions.append((plugin_name, handler_name, schedule))

        r = Roster()
        api = RosterAPI(r, plugin_name="p", on_collision=on_collision)

        assert api.register_recurring("@every 60s", "sweep", {"x": 1}) is True
        assert api.register_recurring("@every 60s", "sweep", {"x": 1}) is False
        assert collisions == [("p", "sweep", "@every 60s")]
        assert len(r) == 1

    def test_different_payload_registers_new_entry(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="p")
        api.register_recurring("@every 60s", "sweep", {"x": 1})
        assert api.register_recurring("@every 60s", "sweep", {"x": 2}) is True
        assert len(r) == 2

    def test_invalid_schedule_raises(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="p")
        with pytest.raises(ValueError):
            api.register_recurring("not a cron", "h")
        with pytest.raises(ValueError):
            api.register_recurring("@every", "h")

    def test_snapshot_is_independent(self) -> None:
        r = Roster()
        api = RosterAPI(r, plugin_name="p")
        api.register_recurring("@every 60s", "h", {})
        snap = api.snapshot()
        snap.clear()
        # Mutating the snapshot must not affect the underlying roster.
        assert len(api.snapshot()) == 1


# ---------------------------------------------------------------------------
# Plugin host build_roster integration
# ---------------------------------------------------------------------------


def test_plugin_host_build_roster_invokes_register_roster(tmp_path: Path) -> None:
    from pollypm.plugin_host import ExtensionHost

    captured: list[str] = []

    def register_roster(api: RosterAPI) -> None:
        captured.append(api.plugin_name)
        api.register_recurring("@every 30s", "inbox.sweep", {"project": "pollypm"})
        api.register_recurring("@on_startup", "boot")

    plugin = PollyPMPlugin(
        name="test_plugin",
        register_roster=register_roster,
    )

    host = ExtensionHost(tmp_path)
    host._plugins = {"test_plugin": plugin}  # bypass manifest discovery

    roster = host.build_roster()
    assert captured == ["test_plugin"]
    assert len(roster) == 2
    handlers = {e.handler_name for e in roster.snapshot()}
    assert handlers == {"inbox.sweep", "boot"}


def test_plugin_host_build_roster_logs_collision(tmp_path: Path, caplog) -> None:
    from pollypm.plugin_host import ExtensionHost

    def register_roster_a(api: RosterAPI) -> None:
        api.register_recurring("@every 60s", "sweep", {"p": "a"})

    def register_roster_b(api: RosterAPI) -> None:
        # Different plugin, same identity → collision on the shared roster.
        api.register_recurring("@every 60s", "sweep", {"p": "a"})

    plugin_a = PollyPMPlugin(name="a", register_roster=register_roster_a)
    plugin_b = PollyPMPlugin(name="b", register_roster=register_roster_b)

    host = ExtensionHost(tmp_path)
    host._plugins = {"a": plugin_a, "b": plugin_b}

    with caplog.at_level(logging.INFO, logger="pollypm.plugin_host"):
        roster = host.build_roster()

    assert len(roster) == 1
    # Collision for plugin b must be logged.
    assert any("register" in rec.message.lower() and "b" in rec.message for rec in caplog.records)


def test_plugin_host_build_roster_captures_hook_failures(tmp_path: Path) -> None:
    from pollypm.plugin_host import ExtensionHost

    def register_roster(api: RosterAPI) -> None:
        raise RuntimeError("plugin exploded")

    plugin = PollyPMPlugin(name="bad", register_roster=register_roster)
    host = ExtensionHost(tmp_path)
    host._plugins = {"bad": plugin}

    roster = host.build_roster()  # must not re-raise
    assert len(roster) == 0
    assert any("bad" in err and "exploded" in err for err in host.errors)


def test_plugin_host_build_roster_skips_plugins_without_hook(tmp_path: Path) -> None:
    from pollypm.plugin_host import ExtensionHost

    plugin_no_hook = PollyPMPlugin(name="silent")  # no register_roster
    plugin_with_hook = PollyPMPlugin(
        name="chatty",
        register_roster=lambda api: api.register_recurring("@every 60s", "h"),
    )

    host = ExtensionHost(tmp_path)
    host._plugins = {"silent": plugin_no_hook, "chatty": plugin_with_hook}

    roster = host.build_roster()
    assert len(roster) == 1
    assert roster.snapshot()[0].handler_name == "h"


def test_roster_api_example_matches_issue_shape() -> None:
    """Confirm the example from issue #162 works as advertised."""
    r = Roster()
    api = RosterAPI(r, plugin_name="example")
    api.register_recurring(
        schedule="*/5 * * * *",
        handler_name="inbox.sweep",
        payload={"project": "pollypm"},
        dedupe_key="inbox.sweep:pollypm",
    )
    entry = r.snapshot()[0]
    assert entry.handler_name == "inbox.sweep"
    assert entry.payload == {"project": "pollypm"}
    assert entry.dedupe_key == "inbox.sweep:pollypm"
