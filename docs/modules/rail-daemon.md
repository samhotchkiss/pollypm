**Last Verified:** 2026-04-22

## Summary

`pm-rail-daemon` is a headless Python process that keeps a `CoreRail` alive so the heartbeat + recovery handlers fire even when the cockpit TUI is closed. Without it, the rail only runs inside the cockpit; if the cockpit exits or crashes, nothing resurrects a dead `pm-operator`, rotates logs, or sweeps stale alerts. The 2026-04-19 outage (operator down 5 hours with no auto-recovery) traces directly to this gap.

The daemon is a tiny supervisor: boot the rail, write a PID file so `pm up` can detect it and `pm reset` can stop it, then block on `signal.pause()` while the rail's own ticker thread does the work.

Touch this module when changing process-level lifecycle (PID file conventions, signal handling, single-owner semantics). Do not add recurring logic here — put it in a recurring handler registered by a plugin.

## Core Contracts

```python
# src/pollypm/rail_daemon.py
def run(config_path: Path, *, poll_interval: float = 60.0) -> int: ...

def _pid_file(home: Path) -> Path: ...
def _claim_pid_file(pid_path: Path) -> bool: ...
def _pid_alive(pid: int) -> bool: ...
```

Exit codes from `run()`:

- `0` — clean shutdown (SIGTERM/SIGINT received, rail stopped, PID file removed).
- `1` — another daemon already owns the PID file.

## File Structure

- `src/pollypm/rail_daemon.py` — daemon entrypoint.
- `pyproject.toml` — exposes `pm-rail-daemon` console script.
- `src/pollypm/cli_features/session_runtime.py` — `pm up` starts the daemon when not already running.
- `tests/test_rail_daemon*.py` — PID handling, signal routing.

## Implementation Details

- **PID file.** `~/.pollypm/rail_daemon.pid` is the single-owner lock. A stale PID (process no longer exists) is overwritten. A live PID belonging to another user is treated as live — we do not steal the slot.
- **Signals.** `SIGTERM` and `SIGINT` trigger `CoreRail.stop()` and remove the PID file. `atexit` ensures the PID file is cleaned up even on uncaught exits.
- **Ticker.** The heartbeat's own ticker thread runs inside the rail; the daemon just keeps the interpreter alive. `poll_interval` is a safety margin for the supervisor loop — it does *not* drive the heartbeat cadence.
- **Config path.** Passed from `pm up`; falls back to `DEFAULT_CONFIG_PATH` (`~/.pollypm/pollypm.toml`).
- **Why a separate process.** The cockpit is a user-interactive TUI that exits on `Ctrl-C`. A background supervisor cannot live in the same process without race conditions on teardown and without blocking the user's exit. A separate daemon also lets `pm` commands attach to / detach from the cockpit freely.

## Related Docs

- [modules/core-rail.md](core-rail.md) — what the daemon actually runs.
- [modules/heartbeat.md](heartbeat.md) — the ticker thread the daemon keeps alive.
- [features/cli.md](../features/cli.md) — `pm up` / `pm down` / `pm reset` that start and stop the daemon.
