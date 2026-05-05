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
            base_dir=tmp_path / ".pollypm",
            logs_dir=tmp_path / ".pollypm/logs",
            snapshots_dir=tmp_path / ".pollypm/snapshots",
            state_db=tmp_path / ".pollypm/state.db",
        ),
        pollypm=PollyPMSettings(controller_account="claude_controller"),
        accounts={
            "claude_controller": AccountConfig(
                name="claude_controller",
                provider=ProviderKind.CLAUDE,
                email="claude@example.com",
                home=tmp_path / ".pollypm/homes/claude_controller",
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


def test_supervisor_registers_itself_as_subsystem(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)
    assert supervisor in supervisor.core_rail.subsystems()


def test_readonly_supervisor_does_not_register(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # Pre-create the DB so readonly mode has something to open.
    Supervisor(config).stop()
    supervisor = Supervisor(config, readonly_state=True)
    assert supervisor not in supervisor.core_rail.subsystems()


def test_corerail_start_invokes_supervisor_lifecycle(tmp_path: Path) -> None:
    """CoreRail.start() must run Supervisor.start() (ensure_layout etc.)."""
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    calls: list[str] = []

    def _ensure_layout() -> Path:
        calls.append("layout")
        return tmp_path

    def _ensure_heartbeat() -> None:
        calls.append("heartbeat")

    def _ensure_knowledge() -> None:
        calls.append("knowledge")

    supervisor.ensure_layout = _ensure_layout  # type: ignore[assignment]
    supervisor.ensure_heartbeat_schedule = _ensure_heartbeat  # type: ignore[assignment]
    supervisor.ensure_knowledge_extraction_schedule = _ensure_knowledge  # type: ignore[assignment]

    supervisor.core_rail.start()

    assert calls == ["layout", "heartbeat", "knowledge"]


def test_corerail_start_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    supervisor = Supervisor(config)

    calls: list[str] = []
    supervisor.ensure_layout = lambda: (calls.append("layout") or tmp_path)  # type: ignore[assignment]
    supervisor.ensure_heartbeat_schedule = lambda: calls.append("hb")  # type: ignore[assignment]
    supervisor.ensure_knowledge_extraction_schedule = lambda: calls.append("kn")  # type: ignore[assignment]

    supervisor.core_rail.start()
    supervisor.core_rail.start()

    assert calls == ["layout", "hb", "kn"]


def test_corerail_stop_reverse_order_with_subsystems(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    host = extension_host_for_root(str(config.project.root_dir.resolve()))
    rail = CoreRail(config, store, host)

    events: list[str] = []

    class _Sub:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def start(self) -> None:
            events.append(f"start:{self.tag}")

        def stop(self) -> None:
            events.append(f"stop:{self.tag}")

    rail.register_subsystem(_Sub("a"))
    rail.register_subsystem(_Sub("b"))
    rail.start()
    rail.stop()

    # subsystems stopped in reverse; state store close happens after
    assert events == ["start:a", "start:b", "stop:b", "stop:a"]


def test_corerail_stop_swallows_subsystem_errors(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    host = extension_host_for_root(str(config.project.root_dir.resolve()))
    rail = CoreRail(config, store, host)

    events: list[str] = []

    class _Boom:
        def start(self) -> None:
            events.append("boom-start")

        def stop(self) -> None:
            events.append("boom-stop")
            raise RuntimeError("boom")

    class _Good:
        def start(self) -> None:
            events.append("good-start")

        def stop(self) -> None:
            events.append("good-stop")

    rail.register_subsystem(_Good())
    rail.register_subsystem(_Boom())
    rail.start()
    rail.stop()  # must not raise

    # Boom stopped first (reverse order); _Good.stop still ran despite boom's error
    assert events == ["good-start", "boom-start", "boom-stop", "good-stop"]


def test_corerail_drives_plugin_host_load(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    host = extension_host_for_root(str(config.project.root_dir.resolve()))

    calls: list[str] = []

    class _HostProxy:
        """Pass-through that records plugins() calls."""

        def __init__(self, inner) -> None:
            self._inner = inner

        def plugins(self):
            calls.append("plugins")
            return self._inner.plugins()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    rail = CoreRail(config, store, _HostProxy(host))
    rail.start()

    assert "plugins" in calls


# ---------------------------------------------------------------------------
# CoreRail boots HeartbeatRail — #184
# ---------------------------------------------------------------------------


def test_corerail_start_retries_transient_first_party_heartbeat_import(
    monkeypatch, tmp_path: Path,
) -> None:
    import pollypm.core.rail as rail_mod

    attempts = 0
    sleeps: list[float] = []
    started: list[bool] = []
    stopped: list[bool] = []

    class _FakeHost:
        def plugins(self):
            return {}

    class _FakeHeartbeatRail:
        def __init__(self) -> None:
            self.roster = type("_Roster", (), {"entries": []})()

        @classmethod
        def from_plugin_host(cls, *, state_db, plugin_host):
            return cls()

        def start(self, *, start_workers: bool = True) -> None:
            started.append(start_workers)

        def stop(self) -> None:
            stopped.append(True)

    def _flaky_load_heartbeat_rail_class():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModuleNotFoundError(
                "No module named 'pollypm.heartbeat'",
                name="pollypm.heartbeat",
            )
        return _FakeHeartbeatRail

    monkeypatch.setattr(
        rail_mod, "_load_heartbeat_rail_class", _flaky_load_heartbeat_rail_class,
    )
    monkeypatch.setattr(rail_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    rail = CoreRail(config, store, _FakeHost())

    rail.start()
    try:
        assert attempts == 2
        assert sleeps == [rail_mod._HEARTBEAT_IMPORT_RETRY_DELAYS[0]]
        assert started == [True]
        assert isinstance(rail.get_heartbeat_rail(), _FakeHeartbeatRail)
    finally:
        rail.stop()
    assert stopped == [True]


def test_corerail_start_does_not_retry_non_first_party_missing_dependency(
    monkeypatch, tmp_path: Path,
) -> None:
    import pollypm.core.rail as rail_mod

    attempts = 0
    sleeps: list[float] = []

    class _FakeHost:
        def plugins(self):
            return {}

    def _load_heartbeat_rail_class():
        nonlocal attempts
        attempts += 1
        raise ModuleNotFoundError(
            "No module named 'third_party_dependency'",
            name="third_party_dependency",
        )

    monkeypatch.setattr(
        rail_mod, "_load_heartbeat_rail_class", _load_heartbeat_rail_class,
    )
    monkeypatch.setattr(rail_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    rail = CoreRail(config, store, _FakeHost())

    rail.start()
    try:
        assert attempts == 1
        assert sleeps == []
        assert rail.get_heartbeat_rail() is None
    finally:
        rail.stop()


def test_corerail_start_boots_heartbeat_rail(tmp_path: Path) -> None:
    """CoreRail.start() constructs + boots a HeartbeatRail so roster-
    registered recurring handlers get drained by a running worker pool.
    """
    import threading

    from pollypm.heartbeat import Roster
    from pollypm.jobs import JobHandlerRegistry

    # Minimal plugin-host stand-in exposing the two methods
    # HeartbeatRail.from_plugin_host relies on.
    registry = JobHandlerRegistry()
    roster = Roster()
    fired: set[str] = set()
    fired_lock = threading.Lock()

    def _mk_handler(name: str):
        def _h(payload: dict) -> dict:
            with fired_lock:
                fired.add(name)
            return {"ok": True}
        return _h

    handler_names = [
        "session.health_sweep",
        "capacity.probe",
        "account.usage_refresh",
        "transcript.ingest",
        "alerts.gc",
        "itsalive.deploy_sweep",
        "briefing.tick",
    ]
    for name in handler_names:
        registry.register(
            name=name, handler=_mk_handler(name),
            plugin_name="test", timeout_seconds=2.0,
        )
        roster.register(schedule="@on_startup", handler_name=name, payload={})

    class _FakeHost:
        def plugins(self):
            return {}

        def build_roster(self):
            return roster

        def job_handler_registry(self):
            return registry

    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    rail = CoreRail(config, store, _FakeHost())

    rail.start()
    try:
        hb = rail.get_heartbeat_rail()
        assert hb is not None, "CoreRail.start() must boot a HeartbeatRail"
        assert hb.pool.is_running, "HeartbeatRail worker pool must be running post-start"

        # Tighten retry backoff + poll interval so the test doesn't linger
        # on transient races; handlers above are all no-ops.
        from pollypm.jobs import exponential_backoff
        hb.queue.retry_policy = exponential_backoff(
            base_seconds=0.01, factor=1.0, max_seconds=0.01, jitter=0,
        )
        hb.pool.poll_interval = 0.02

        # Two ticks — first enqueues the @on_startup entries; second is a
        # no-op (on_startup_fired is set). The worker pool drains asynchronously.
        from datetime import UTC, datetime
        result1 = hb.tick(datetime.now(UTC))
        assert result1.enqueued_count == len(handler_names)
        result2 = hb.tick(datetime.now(UTC))
        assert result2.enqueued_count == 0  # on_startup already fired

        import time
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with fired_lock:
                if len(fired) == len(handler_names):
                    break
            time.sleep(0.02)
        with fired_lock:
            missing = set(handler_names) - fired
        assert not missing, f"Handlers that did not fire within 5s: {missing}"
    finally:
        rail.stop()
        # After stop(), the HeartbeatRail reference is cleared.
        assert rail.get_heartbeat_rail() is None


def test_corerail_stop_tears_down_heartbeat_rail(tmp_path: Path) -> None:
    """CoreRail.stop() must stop the HeartbeatRail's worker pool."""
    from pollypm.heartbeat import Roster
    from pollypm.jobs import JobHandlerRegistry

    class _FakeHost:
        def plugins(self):
            return {}

        def build_roster(self):
            return Roster()

        def job_handler_registry(self):
            return JobHandlerRegistry()

    config = _config(tmp_path)
    store = StateStore(config.project.state_db)
    rail = CoreRail(config, store, _FakeHost())
    rail.start()
    hb = rail.get_heartbeat_rail()
    assert hb is not None
    pool = hb.pool
    assert pool.is_running
    rail.stop()
    assert not pool.is_running
