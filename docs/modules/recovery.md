**Last Verified:** 2026-04-22

## Summary

`RecoveryPolicy` is the sealed classifier + escalation ladder. Given `SessionSignals` (pane state, work-service status, capacity, intervention history), the policy returns a `SessionHealth` classification and — when an intervention is warranted — an `InterventionAction`. The Supervisor applies the action; the policy only decides.

The shipped `DefaultRecoveryPolicy` (`src/pollypm/recovery/default.py`) implements the core ladder `nudge → reset → relaunch → failover → escalate` plus the work-service-aware classifications `stuck_on_task`, `silent_worker`, and `state_drift` introduced in issues #249 / #296. The policy is **stateless** — it reads intervention history from `SessionSignals.intervention_history` and does not memoize anything on the instance.

Touch this module when the ladder changes, when a new classification lands, or when tuning thresholds (e.g. `STUCK_ON_TASK_SECONDS`, `SILENT_WORKER_SECONDS` = 1800s each). Do not add execution side effects — those belong in the Supervisor.

## Core Contracts

```python
# src/pollypm/recovery/base.py
class SessionHealth(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    STUCK = "stuck"
    LOOPING = "looping"
    EXITED = "exited"
    ERROR = "error"
    BLOCKED_NO_CAPACITY = "blocked_no_capacity"
    AUTH_BROKEN = "auth_broken"
    WAITING_ON_USER = "waiting_on_user"
    HEALTHY = "healthy"
    STUCK_ON_TASK = "stuck_on_task"       # #249
    SILENT_WORKER = "silent_worker"       # #249
    STATE_DRIFT = "state_drift"           # #296

INTERVENTION_LADDER = ("nudge", "reset", "relaunch", "failover", "escalate")
EXTENDED_INTERVENTIONS = ("resume_ping", "prompt_pm_task_next", "reconcile_flow_state")

@dataclass(slots=True)
class SessionSignals:
    session_name: str
    window_present: bool = True
    pane_alive: bool = True
    pane_dead: bool = False
    pane_command: str | None = None
    pane_text: str = ""
    last_heartbeat_at: datetime | None = None
    capacity: CapacityState | None = None
    intervention_history: tuple[InterventionHistoryEntry, ...] = ()
    # plus work-service-aware fields: worker_session, task_status, last_transition_at, ...

@dataclass(slots=True)
class InterventionHistoryEntry:
    action: str
    timestamp: datetime
    reason: str | None = None

@dataclass(slots=True)
class InterventionAction:
    action: str
    target_account: str | None = None
    message: str | None = None
    reason: str | None = None

class RecoveryPolicy(Protocol):
    name: str
    def classify(self, signals: SessionSignals) -> SessionHealth: ...
    def select_intervention(
        self, health: SessionHealth, signals: SessionSignals,
    ) -> InterventionAction | None: ...
```

## File Structure

- `src/pollypm/recovery/__init__.py` — re-exports.
- `src/pollypm/recovery/base.py` — protocol + data types + ladder constants.
- `src/pollypm/recovery/default.py` — `DefaultRecoveryPolicy` (classifier + ladder).
- `src/pollypm/recovery/pane_patterns.py` — regex library for detecting provider-specific idle / loop / error states.
- `src/pollypm/recovery/state_reconciliation.py` — work-service-aware drift detection for `STATE_DRIFT` (#296).
- `src/pollypm/recovery/worker_turn_end.py` — worker-turn-end detection heuristics.
- `src/pollypm/plugins_builtin/default_recovery_policy/plugin.py` — registers `DefaultRecoveryPolicy` as `"default"`.
- `tests/test_recovery_policy.py` — pins the contract.
- `src/pollypm/capacity.py` — `CapacityState` + `FAILOVER_TRIGGERS` used by the policy.

## Implementation Details

- **Stateless.** Instance state is limited to `name` and tunable thresholds. Everything else comes from `SessionSignals` each call.
- **Classification precedence.** `EXITED` and `ERROR` dominate. `AUTH_BROKEN` and `BLOCKED_NO_CAPACITY` are checked before liveness heuristics. `STUCK_ON_TASK` / `SILENT_WORKER` apply when the worker is bound to a `work_status=in_progress` task but has produced no pane output or state transition for ≥ 1800 seconds. `WAITING_ON_USER` triggers on operator-side prompts.
- **Operator exemption.** The operator session is allowed to sit idle or waiting on user input without an intervention. The classifier special-cases `session.role == "operator-pm"`.
- **Ladder advancement.** The policy walks `INTERVENTION_LADDER` in order, skipping rungs already attempted in `intervention_history` within the current incident. If the top rung is `escalate` and another `escalate` already happened inside the window, the policy returns `None` so the Supervisor does not spam alerts.
- **Extended interventions.** `resume_ping`, `prompt_pm_task_next`, and `reconcile_flow_state` are *not* part of the sequential ladder — they are paired with the work-service-aware classifications and can fire in parallel. `STUCK_ON_TASK` typically pairs with `resume_ping` or `prompt_pm_task_next`; `STATE_DRIFT` pairs with `reconcile_flow_state` (alert + event only, no auto-advance in v1).
- **Dedupe alignment.** `STUCK_ON_TASK_SECONDS = 1800` and `SILENT_WORKER_SECONDS = 1800` mirror the `task_assignment_notify` dedupe window so a `stuck_on_task` classification never out-races a resume ping.

## Related Docs

- [modules/supervisor.md](supervisor.md) — applies the policy's decisions.
- [modules/session-services.md](session-services.md) — supplies the pane state.
- [modules/heartbeat.md](heartbeat.md) — `session.health_sweep` drives classification.
- [modules/accounts.md](accounts.md) — supplies capacity + runtime account status that feed failover.
