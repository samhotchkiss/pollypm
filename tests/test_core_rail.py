"""Tests for the CoreRail skeleton.

These cover only Step 1 behavior: construction, accessors, event bus
no-op, and subsystem registration plumbing. The rail does not yet own
Supervisor's startup — those tests land in Step 2.
"""

from __future__ import annotations

from pathlib import Path

from pollypm.core import CoreRail, Startable
from pollypm.models import (
    AccountConfig,
    KnownProject,
    ProjectKind,
    ProjectSettings,
    PollyPMConfig,
    PollyPMSettings,
    ProviderKind,
)
from pollypm.plugin_host import extension_host_for_root
from pollypm.storage.state import StateStore
from pollypm.supervisor import Supervisor


def _config(tmp_path: Path) -> PollyPMConfig:
    return PollyPMConfig(
        project=ProjectSettings(
            root_dir=tmp_path,
            base_dir=tmp_path / ".pollypm-state",
            logs_dir=tmp_path / ".pollypm-state/logs",
            snapshots_dir=tmp_path / ".pollypm-state/snapshots",
            state_db=tmp_path / ".pollypm-state/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm-state/homes/claude_controller",
            ),
        },
        sessions={},
        projects={
            "pollypm": KnownProject(
                key="pollypm",
                path=tmp_path,
                name="PollyPM",
                kind=ProjectKind.FOLDER,
            )
        },
    )


def _rail(tmp_path: Path) -> CoreRail:
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    host = extension_host_for_root(str(config.project.root_dir.resolve()))
    return CoreRail(config, store, host)


def test_corerail_construction(tmp_path: Path) -> None:
    rail = _rail(tmp_path)
    assert rail is not None


def test_corerail_accessors_roundtrip(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    host = extension_host_for_root(str(config.project.root_dir.resolve()))
    rail = CoreRail(config, store, host)

    assert rail.get_config() is config
    assert rail.get_state_store() is store
    assert rail.get_plugin_host() is host


def test_corerail_emit_event_is_callable(tmp_path: Path) -> None:
    rail = _rail(tmp_path)
    # Should not raise with either empty or populated payload.
    rail.emit_event("test.event", {})
    rail.emit_event("test.event", {"k": "v", "n": 1})


def test_corerail_startable_protocol_recognizes_objects(tmp_path: Path) -> None:
    class _Sub:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    assert isinstance(_Sub(), Startable)


def test_corerail_register_and_start_stop_invokes_in_order(tmp_path: Path) -> None:
    rail = _rail(tmp_path)
    events: list[str] = []

    class _Sub:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def start(self) -> None:
            events.append(f"start:{self.tag}")

        def stop(self) -> None:
            events.append(f"stop:{self.tag}")

    a, b = _Sub("a"), _Sub("b")
    rail.register_subsystem(a)
    rail.register_subsystem(b)
    rail.start()
    rail.stop()

    assert events == ["start:a", "start:b", "stop:b", "stop:a"]


def test_corerail_register_is_idempotent(tmp_path: Path) -> None:
    rail = _rail(tmp_path)

    class _Sub:
        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    sub = _Sub()
    rail.register_subsystem(sub)
    rail.register_subsystem(sub)
    assert rail.subsystems() == [sub]


def test_supervisor_constructs_default_corerail(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    assert supervisor.core_rail is not None
    assert supervisor.core_rail.get_config() is config
    assert supervisor.core_rail.get_state_store() is supervisor.store


def test_supervisor_reuses_injected_corerail(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    host = extension_host_for_root(str(config.project.root_dir.resolve()))
    rail = CoreRail(config, store, host)

    supervisor = Supervisor(config, core_rail=rail)

    assert supervisor.core_rail is rail
    assert supervisor.store is store
