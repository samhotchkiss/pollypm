# Chaos Injectors

Issue #2466 adds three sandboxed injectors for the 48h watchdog run.
They live under `scripts/chaos/` with self-tests in `tests/chaos/`.

Run dry-run checks with:

```bash
uv run --extra test python -m scripts.chaos.run failover
uv run --extra test python -m scripts.chaos.run session-kill
uv run --extra test python -m scripts.chaos.run task-stall --dsn postgresql://localhost:5432/pollypm_test
```

`task-stall --execute` requires an isolated test DSN. The harness calls
`tests.conftest_pg._is_ambient_live_pg_dsn` and refuses the default local
operator database shape (`postgresql://localhost:5432/pollypm`).

Mappings:

| Injector | Broken state | Detector / recovery mapping |
| --- | --- | --- |
| `failover` | Sandbox heartbeat context emits auth-broken or usage-limit text. | Live heartbeat watchdog path: `LocalHeartbeatBackend._handle_auth_failure` / `_handle_capacity_failure`, then `DefaultRecoveryPolicy.classify/select_intervention` and `Supervisor.maybe_recover_session`. There is no audit-log `_detect_*` rule for account failover. |
| `session-kill` | Sandbox role window missing for an in-progress task. | `pollypm.audit.watchdog._detect_role_session_missing` (`role_session_missing`). Recovery evidence is the finding clearing when the sandbox window is reconciled present. |
| `task-stall` | Isolated PG fixture task aged past `progress_stale_seconds` while `in_progress`. | `pollypm.audit.watchdog._detect_task_progress_stale` (`task_progress_stale`). Recovery evidence is `work_service.release_stale_claim` moving the task back to `queued` and clearing the finding. |

Every injector emits structured JSON with `pre_recovery`, `post_recovery`,
`mapped_rule`, `safety`, and `passed` fields.
