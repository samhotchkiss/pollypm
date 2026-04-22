**Last Verified:** 2026-04-22

## Summary

`CoreRail` is the process-wide lifecycle owner. It holds exactly three references — `PollyPMConfig`, `StateStore`, and `ExtensionHost` (the plugin host) — and drives subsystem start/stop in a deterministic order. The rail is additive today: `Supervisor` can be constructed without a rail and will build one internally. Outside of tests, a process should construct one `CoreRail` per boot (see `pollypm.rail_daemon` and `cockpit_ui.CockpitRail._start_core_rail`) and register subsystems against it.

The rail also boots the `HeartbeatRail` at startup — the sealed JobQueue + JobWorkerPool + Heartbeat trio that drains roster-registered recurring handlers. A failure to boot the HeartbeatRail is logged but does not abort the rest of the rail: CLI, cockpit, and Supervisor tick still function with degraded recurring handlers.

Touch this module when adding a subsystem with its own lifecycle, or when changing boot ordering. Do not reach into `_state_store` / `_plugin_host` directly — use the accessors.

## Core Contracts

```python
# src/pollypm/core/rail.py
@runtime_checkable
class Startable(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...

class CoreRail:
    def __init__(
        self,
        config: PollyPMConfig,
        state_store: StateStore,
        plugin_host: ExtensionHost,
    ) -> None: ...

    def get_config(self) -> PollyPMConfig: ...
    def get_state_store(self) -> StateStore: ...
    def get_plugin_host(self) -> ExtensionHost: ...
    def emit_event(self, name: str, payload: dict) -> None: ...

    def register_subsystem(self, subsystem: Startable) -> None: ...
    def subsystems(self) -> list[Startable]: ...

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def get_heartbeat_rail(self) -> HeartbeatRail | None: ...
```

Boot order (enforced in `start()`):

1. Plugin host readiness — eager plugin load to surface errors early.
2. State store readiness — logged only; the store is opened and migrated in its constructor.
3. Subsystems in registration order (Supervisor first, then heartbeat workers as registered).
4. `HeartbeatRail` boot — constructs sealed JobQueue + JobWorkerPool + Heartbeat and starts the ticker thread.

Shutdown is strict reverse order. `start()` and `stop()` are idempotent.

## File Structure

- `src/pollypm/core/__init__.py` — exports `CoreRail` and `Startable`.
- `src/pollypm/core/rail.py` — the rail implementation.
- `src/pollypm/core/console_window.py` — cockpit console-window manager (registered as a rail subsystem).
- `src/pollypm/rail_daemon.py` — headless process that constructs a rail and keeps it ticking.
- `src/pollypm/heartbeat/boot.py` — `HeartbeatRail`, constructed by `_start_heartbeat_rail`.

## Implementation Details

- The rail is kept minimal on purpose. It is *not* the event bus (`emit_event` is a debug-log placeholder). Inter-subsystem communication still runs through the existing channels — session bus, task assignment bus, plugin host hooks.
- Subsystem registration deduplicates by identity. Re-registering the same instance is a silent no-op so plugin code that registers from multiple hooks does not shift start order.
- The `HeartbeatRail` ticker runs in a daemon thread with a default 15-second interval (`DEFAULT_TICK_INTERVAL_SECONDS` in `heartbeat/boot.py`). `[heartbeat.workers].concurrency` in `pollypm.toml` sets the worker pool size; default 4.
- `CoreRail.start()` currently **does not** reach into Supervisor boot. The Supervisor is a registered subsystem; the rail calls `supervisor.start()` in order like any other.
- `get_heartbeat_rail()` returns `None` when the HeartbeatRail failed to boot. Callers must tolerate this so a partial rail can still serve reads.

## Related Docs

- [modules/heartbeat.md](heartbeat.md) — the sealed heartbeat boot artifacts.
- [modules/supervisor.md](supervisor.md) — primary subsystem.
- [modules/rail-daemon.md](rail-daemon.md) — headless CoreRail host.
- [modules/plugin-host.md](plugin-host.md) — `ExtensionHost` held by the rail.
