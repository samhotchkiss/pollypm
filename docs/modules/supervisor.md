**Last Verified:** 2026-04-22

## Summary

`Supervisor` is the orchestrator for the moving parts of PollyPM's runtime. It used to own *everything* (tmux, launches, recovery, heartbeats, leases, checkpoints, alerts) and is in **late-stage decomposition**: issues #179 and #186 peeled off `CoreRail`, `HeartbeatRail`, `DefaultLaunchPlanner`, `DefaultRecoveryPolicy`, `TmuxSessionService`, and the cockpit console-window manager. What remains is tmux bootstrap + layout, session stabilization, recovery interventions, and the `send_input` surface.

External callers **must** route through `pollypm.service_api.PollyPMService` — `tests/test_import_boundary.py` enforces this with an allow-list. Inside `pollypm.core` and the remaining legacy callers the allow-list permits direct imports, but every new call site should go through the facade.

Touch this module only when you are extracting another subsystem. Do not add new responsibilities — add a collaborator and route the Supervisor through it.

## Core Contracts

`Supervisor` takes a `PollyPMConfig` and exposes a pragmatic surface the service facade wraps:

```python
# src/pollypm/supervisor.py
class Supervisor:
    def __init__(
        self,
        config: PollyPMConfig,
        *,
        readonly_state: bool = False,
        rail: CoreRail | None = None,
        launch_planner: LaunchPlanner | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        session_service: SessionService | None = None,
        heartbeat_backend: HeartbeatBackend | None = None,
    ) -> None: ...

    # Boot / status
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def bootstrap_tmux(self, ...) -> None: ...
    def shutdown_tmux(self) -> None: ...
    def ensure_layout(self) -> Path: ...
    def status(self) -> tuple[
        list[SessionLaunchSpec], list[TmuxWindow],
        list[AlertRecord], list[LeaseRecord], list[str],
    ]: ...
    def run_heartbeat(self, snapshot_lines: int = 200) -> list[AlertRecord]: ...

    # Panes
    def send_input(self, session_name, text, *, owner="pollypm", force=False) -> None: ...
    def focus_session(self, session_name) -> None: ...
    def focus_console(self) -> None: ...

    # Leases
    def claim_lease(self, session_name, owner, note="") -> None: ...
    def release_lease(self, session_name, expected_owner=None) -> None: ...
    def release_expired_leases(self, *, now=None) -> list[LeaseRecord]: ...
    def leases(self) -> list[LeaseRecord]: ...

    # Sessions
    def stop_session(self, session_name) -> None: ...
    def switch_session_account(self, session_name, account_name) -> None: ...

    # Alerts
    def open_alerts(self) -> list[AlertRecord]: ...
```

The Supervisor is **not** a protocol — it is the concrete orchestrator. Collaborators (launch planner, recovery policy, session service, heartbeat backend) are injected and can be swapped, but the Supervisor class itself is instantiated as-is. Alert / session-status / set-session-status operations are available on `PollyPMService` (see [features/service-api.md](../features/service-api.md)) and route through the state store directly rather than the Supervisor.

## File Structure

- `src/pollypm/supervisor.py` — the orchestrator.
- `src/pollypm/supervisor_alerts.py` — alert formatting and dedupe helpers.
- `src/pollypm/core/rail.py` — `CoreRail`, which owns the Supervisor as a subsystem.
- `src/pollypm/launch_planner/base.py` + `src/pollypm/plugins_builtin/default_launch_planner/` — the split-out planner.
- `src/pollypm/recovery/base.py` + `default.py` — the split-out recovery policy.
- `src/pollypm/session_services/tmux.py` — the split-out tmux mechanics.
- `src/pollypm/heartbeats/local.py` + `api.py` — the split-out heartbeat backend.
- `tests/test_import_boundary.py` — enforces the Supervisor allow-list.

## Implementation Details

- **Decomposition status.** Status after Step 8 of the split (see the module docstring at `src/pollypm/supervisor.py:20`). Further issues will peel off the remaining responsibilities one at a time; renaming the class to `LegacySupervisor` was considered in #186 and deferred — the remaining surface is big enough that a bulk rename would ripple without architectural benefit.
- **Supervisor vs. service facade.** `PollyPMService` constructs a fresh Supervisor per call with `load_supervisor(readonly_state=...)`. This keeps the facade side-effect-free. The Supervisor is not expected to be kept alive across operations by facade consumers.
- **Session lifecycle.** Tmux session creation is idempotent — `ensure_tmux_session` checks for the configured session (`pollypm` by default) and creates or attaches. Window creation goes through `SessionService.create_window(...)` against the active session.
- **Read-only mode.** `readonly_state=True` passes through to `StateStore` so status reads don't race with writes.
- **The `send_input` surface** still lives here because the tmux bootstrap owns the pane lease and owner-prefix rules (`_OWNER_PREFIXES` maps `"heartbeat"`, `"polly"`, `"pollypm"`, `"operator"` to log prefixes). This belongs in `SessionService` long-term but hasn't moved yet.
- **Recovery.** `Supervisor.run_heartbeat` samples pane state, asks the `RecoveryPolicy` to classify each session, and applies the policy's recommended intervention (nudge / reset / relaunch / failover / escalate). See [modules/recovery.md](recovery.md).

## Related Docs

- [features/service-api.md](../features/service-api.md) — the stable facade.
- [modules/core-rail.md](core-rail.md) — owns the Supervisor as a subsystem.
- [modules/recovery.md](recovery.md) — classification and intervention ladder.
- [modules/session-services.md](session-services.md) — tmux service boundary.
