**Last Verified:** 2026-04-22

## Summary

A `SessionService` owns tmux mechanics: create windows, capture panes, send input, list sessions, dispatch session-lifecycle events. It is **dumb infrastructure** — it does what it's told and makes no policy decisions. Policy (when to recover, when to fail over, when to escalate) lives in the Supervisor and `RecoveryPolicy`.

The only shipped implementation is `TmuxSessionService` (`src/pollypm/session_services/tmux.py`), wired by the `tmux_session_service` plugin. The protocol is open for alternative terminal multiplexers or headless containers.

The module also exposes a tiny **session-lifecycle event bus**. `SessionCreatedEvent` fires when a fresh session becomes stable; the `task_assignment_notify` plugin subscribes to replay outstanding worker pings (issue #246). No protocol change needed — implementations that don't dispatch fall back to the 30-second sweeper.

Touch this module when you need new tmux mechanics (e.g. new layout primitives) or when adding another session-service backend. Do not add policy here.

## Core Contracts

```python
# src/pollypm/session_services/base.py
@dataclass(slots=True)
class SessionHandle:
    name: str
    provider: str
    account: str
    window_name: str
    pane_id: str | None
    tmux_session: str
    cwd: str
    log_path: Path | None = None

@dataclass(slots=True)
class SessionHealth:
    window_present: bool
    pane_alive: bool
    pane_dead: bool
    pane_command: str | None
    pane_text: str

@dataclass(slots=True)
class TranscriptStream:
    path: Path
    offset: int = 0
    delta: str = ""

class SessionService(Protocol):
    name: str
    def create(self, name, provider, account, cwd, ...) -> SessionHandle: ...
    def destroy(self, name: str) -> None: ...
    def capture(self, name: str, *, lines: int = 200) -> SessionHealth: ...
    def send(self, name: str, text: str, *, owner: str = "pollypm") -> None: ...
    def list(self) -> list[SessionHandle]: ...

# Session lifecycle bus
def register_session_listener(listener: Callable[[SessionCreatedEvent], None]) -> None: ...
def dispatch_session_event(event: SessionCreatedEvent) -> None: ...

# Convenience helpers re-exported from the package
def create_tmux_client(...) -> TmuxClient: ...
def attach_existing_session(session_name: str) -> int: ...
def current_session_name() -> str | None: ...
def probe_session(session_name: str) -> bool: ...
def switch_client_to_session(session_name: str) -> int: ...
```

## File Structure

- `src/pollypm/session_services/__init__.py` — re-exports protocol, helpers, lifecycle bus.
- `src/pollypm/session_services/base.py` — `SessionService` protocol, `SessionHandle`, `SessionHealth`, `TranscriptStream`, lifecycle event bus.
- `src/pollypm/session_services/tmux.py` — `TmuxSessionService` — the default implementation.
- `src/pollypm/tmux/client.py` — thin wrapper around `tmux` CLI (`TmuxWindow`, command runners).
- `src/pollypm/plugins_builtin/tmux_session_service/plugin.py` — registers the factory as session service `"tmux"`.

## Implementation Details

- **Pane logging.** Every session gets `tmux pipe-pane -o` streaming raw pane output to `<project>/.pollypm/logs/<session>/<launch-id>/pane.log`. Log rotation runs hourly via `core_recurring.log.rotate`.
- **Pane capture.** `capture` runs `tmux capture-pane -pS -<lines>` to produce the pane text the heartbeat uses for classification. Snapshots are written to `snapshots/` under the launch directory at recurring intervals.
- **`owner` prefix.** `send(name, text, owner=...)` prepends a prefix (`H:` heartbeat, `P:` polly/operator, `[PollyPM]` pollypm) so a human scrolling the pane can see who typed. The mapping lives in `supervisor.py:_OWNER_PREFIXES`.
- **Lease model.** The Supervisor's lease table (see [modules/state-store.md](state-store.md)) arbitrates human/automation ownership. Automation calls `send` with its owner label; humans attaching to the pane implicitly take over. `release_expired_leases` (runs every 5 minutes via `alerts.gc`) reclaims leases older than `lease_timeout_minutes` (default 30).
- **`SessionCreatedEvent` dispatch.** `TmuxSessionService.create` calls `dispatch_session_event` once the tmux window is alive and the pane has emitted its first bytes. Subscribers run synchronously; long-running work belongs in a job handler.

## Related Docs

- [modules/supervisor.md](supervisor.md) — still owns parts of `send_input` and session stabilization.
- [modules/recovery.md](recovery.md) — consumes `SessionHealth` for classification.
- [plugins/task-assignment-notify.md](../plugins/task-assignment-notify.md) — subscribes to `SessionCreatedEvent`.
- [modules/transcript-ingest.md](transcript-ingest.md) — picks up provider JSONL that sits alongside pane logs.
