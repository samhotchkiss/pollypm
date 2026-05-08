# Audit Log

PollyPM writes an append-only audit log for high-signal state mutations:
task lifecycle events, worker marker lifecycle events, work database opens,
watchdog findings, and a small set of operational recovery events. It exists
for two audiences: operators who need to reconstruct what happened after a
broken run, and contributors who need a stable event stream for heartbeat and
diagnostic rules.

The implementation lives in `src/pollypm/audit/`. The public writer and reader
are exported from `pollypm.audit`, and the stable event-name constants live in
`src/pollypm/audit/log.py`.

## On-Disk Layout

Each event is written as one compact JSON object per line.

| Location | Purpose |
|---|---|
| `<project>/.pollypm/audit.jsonl` | Project-local source of truth when the project path is known. |
| `~/.pollypm/audit/<project>.jsonl` | Central tail used by heartbeat, doctor-style tooling, and quick operator searches. |

Tests and specialized tools can relocate the central tail with
`POLLYPM_AUDIT_HOME`. The project-local file is not affected by that override.

The writer is best-effort. Audit failure must not block the mutation that
caused it, so callers wrap emits defensively and `emit()` also swallows write
errors after logging a warning.

## Rotation And Retention

There is no automatic rotation for audit JSONL files today. The
[log rotation work in PR #1384](https://github.com/samhotchkiss/pollypm/pull/1384)
covers `~/.pollypm/*.log` process logs such as `errors.log`,
`cockpit_debug.log`, `rail_daemon.log`, and `phantom_client.log`; it does not
cover `~/.pollypm/audit/*.jsonl` or `<project>/.pollypm/audit.jsonl`.

The `[events] audit_retention_days` setting applies to database `messages`
rows categorized as audit events, not to these JSONL files. Until a dedicated
audit-log rotation pass lands, operators should archive or truncate audit JSONL
files manually after preserving any incident window they still need.

## Event Schema

Every audit line uses schema version 1:

```json
{
  "schema": 1,
  "ts": "2026-05-06T17:26:44.123456+00:00",
  "project": "demo",
  "event": "task.status_changed",
  "subject": "demo/7",
  "actor": "worker",
  "status": "ok",
  "metadata": {
    "from": "queued",
    "to": "in_progress",
    "reason": "claimed by heartbeat"
  }
}
```

Fields:

| Field | Meaning |
|---|---|
| `schema` | Integer schema version. Version 1 is the current on-disk shape. |
| `ts` | ISO-8601 UTC timestamp emitted by the writer. |
| `project` | Project key. Workspace-level events use synthetic keys such as `_workspace`. |
| `event` | Stable event name, usually `noun.verb`. Watchdog rules and tests pin these strings. |
| `subject` | Event target, commonly `project/N`, a marker path, a DB path, or a session name. |
| `actor` | User, agent, system component, or empty string when unknown. |
| `status` | `ok`, `warn`, or `error`. |
| `metadata` | Event-specific JSON object. Keep values JSON-serializable and compact. |

Common lifecycle examples:

```json
{"schema":1,"ts":"2026-05-06T17:26:44.123456+00:00","project":"demo","event":"task.status_changed","subject":"demo/7","actor":"worker","status":"ok","metadata":{"from":"queued","to":"in_progress","reason":"claimed by heartbeat"}}
```

```json
{"schema":1,"ts":"2026-05-06T17:27:01.000000+00:00","project":"demo","event":"marker.created","subject":"/path/to/demo/.pollypm/worker-markers/task-demo-7.fresh","actor":"worker","status":"ok","metadata":{"window_name":"task-demo-7","marker_kind":"fresh","error":null}}
```

```json
{"schema":1,"ts":"2026-05-06T17:27:04.000000+00:00","project":"demo","event":"marker.released","subject":"/path/to/demo/.pollypm/worker-markers/task-demo-7.fresh","actor":"worker","status":"ok","metadata":{"window_name":"task-demo-7","session_role":"worker","error":null}}
```

```json
{"schema":1,"ts":"2026-05-06T17:27:10.000000+00:00","project":"_workspace","event":"work_db.opened","subject":"/workspace/.pollypm/state.db","actor":"system","status":"ok","metadata":{"had_messages_table_pre_open":false,"tables_created":false,"project_path":"/path/to/demo"}}
```

## Core Events

The current event families are:

| Event | Emitted by | Key metadata |
|---|---|---|
| `task.created` | `SQLiteWorkService.create_task` helper | `title`, `type`, `flow_template`, `priority`, `requires_human_review` |
| `task.status_changed` | work-service transition writer | `from`, `to`, `reason` |
| `task.deleted` | work-service prune path | `reason`, `flow_template`, `title` |
| `marker.created` | worker provisioning | `window_name`, `marker_kind`, `error` |
| `marker.create_failed` | worker provisioning failure path | `window_name`, `marker_kind`, `error` |
| `marker.released` | tmux session service after kickoff delivery | `window_name`, `session_role`, `error` |
| `marker.leaked` | tmux identity/persona guard branches | `window_name`, `session_role`, `error` |
| `work_db.opened` | `SQLiteWorkService.__init__` | `had_messages_table_pre_open`, `tables_created`, `project_path` |
| `work_table.cleared` | Reserved for future wholesale work-table reset paths | reset reason and affected scope |
| `heartbeat.tick` | audit watchdog cadence | cadence metadata |
| `audit.finding` | audit watchdog findings | `rule`, `message`, `recommendation`, plus rule-specific data |
| `worker.session_reaped` | worker marker reaper | `window_name`, `marker_path`, `reason` |
| `watchdog.escalation_dispatched` | auto-unstick dispatch path | `finding_type`, `subject`, `brief` |
| `session.provisioned` | reviewer/role recovery provisioning | `role`, `project`, `reason` |
| `worker.thread_leaked` | job worker shutdown leak detector | worker/thread diagnostic metadata |
| `socket.reaped` | cockpit socket reaper | stale socket diagnostics |
| `daemon.reaped` | rail daemon reaper | `role`, `pid`, `age_s`, `reason` |
| `plan.version_incremented` | plan refinement | `task_id`, `old_version`, `new_version`, optional `reason` |
| `plan.successor_created` | replan successor creation | `predecessor`, `successor` |

`work_db.opened` is the dual-DB breadcrumb. It helps distinguish a normal work
DB open from a caller accidentally stamping work tables onto the messages-side
database. See [Work Service Specification](work-service-spec.md) for the
current work-service factory and DB-resolution model.

## Watchdog Rules

The audit watchdog is registered by the built-in core recurring plugin as
`audit.watchdog` on an `@every 5m` schedule. The handler emits a
`heartbeat.tick`, reads each known project's audit log, runs pure detectors in
`pollypm.audit.watchdog`, emits `audit.finding` for every finding, and upserts
an alert keyed by `(rule, project, subject)`.

| Rule | Detects | Default threshold | Operator response |
|---|---|---|---|
| `orphan_marker` | `marker.created` without matching `marker.released` and without a terminal task transition. | `window_seconds=1800` | Inspect the worker pane; resume it or transition/cancel the task so cleanup can release the marker. |
| `marker_leaked` | Any `marker.leaked` event in the window. | `window_seconds=1800` | Investigate identity/persona guard failures and session-name collisions. |
| `stuck_draft` | A draft task that has not moved to queued, in progress, review, blocked, rework, or a terminal state. | `stuck_draft_seconds=300` | Queue the task with `pm task queue <project/N>` or cancel it if it is stale. |
| `cancellation_no_promotion` | A task was cancelled and no later `task.created` event appeared for the same project within the grace window. | `cancel_grace_seconds=300` | Nudge or restart planning so a replacement task is created, or confirm cancellation was intentional. |
| `task_review_stale` | A task has stayed at `status=review` too long. | `review_stale_seconds=1800` | Spawn a reviewer or complete/reject the task manually. |
| `task_on_hold_stale` | A task has stayed at `status=on_hold` too long. | `on_hold_stale_seconds=900` | Let the architect reassess. Use an on-hold reason starting with `human-needed` only when a real human decision is required. |
| `role_session_missing` | A queued, in-progress, or review task needs a role window that is absent from the storage closet. | No time threshold; depends on live task state and tmux windows. | Spawn the expected role session or reassign the task. |
| `worker_session_dead_loop` | Repeated `worker.session_reaped` events for the same task. | `dead_loop_threshold=3` within `dead_loop_window_seconds=600` | Inspect the task and underlying spawn failure; cancel, reassign, or fix the project issue. |

Several auto-unstick rules can dispatch a structured brief to the project's
architect. Dispatch is throttled for 30 minutes by reading prior
`watchdog.escalation_dispatched` events from the audit log itself.

There is no ignore-list or permanent silence knob. To silence a false positive,
make the state true again: release/reap the marker, queue or cancel the draft,
create the intended replacement task, spawn the missing role session, or add a
correct transition. For threshold-only false positives in tests or custom
sweeps, pass a `WatchdogConfig` or handler payload with longer thresholds.

## Morning Briefing Signal Gate

The morning briefing uses the audit log as a cheap project-activity gate before
running heavier git and SQL probes. A tracked project is included when it has
any audit event in the briefing window except `heartbeat.tick`. Heartbeat-only
or empty logs are skipped so dormant projects do not expand the briefing into
empty sections.

To force the briefing to include every tracked project, set:

```bash
POLLYPM_BRIEFING_INCLUDE_ALL=1 pm briefing preview
```

The internal gatherer also accepts `include_quiet_projects=True`, which tests
and diagnostic callers can use to bypass the gate. See
[Morning Briefing Plugin Specification](morning-briefing-plugin-spec.md) for
the rest of the briefing workflow.

## Operator Recipes

Tail the central audit tail for a project:

```bash
tail -f ~/.pollypm/audit/<project>.jsonl
```

Tail the project-local source of truth:

```bash
tail -f /path/to/project/.pollypm/audit.jsonl
```

Pretty-print the latest events:

```bash
jq . /path/to/project/.pollypm/audit.jsonl | less
```

Filter by event with `jq`:

```bash
jq 'select(.event == "audit.finding")' ~/.pollypm/audit/<project>.jsonl
```

Read through the Python API:

```python
from pollypm.audit import read_events

events = read_events("demo", event="task.status_changed", limit=20)
for event in events:
    print(event.ts, event.subject, event.metadata)
```

Clear or archive only after you have preserved anything needed for an incident:

```bash
gzip -c ~/.pollypm/audit/<project>.jsonl > ~/Desktop/<project>-audit.jsonl.gz
: > ~/.pollypm/audit/<project>.jsonl
```

Prefer truncation over deletion when a process may be tailing the file. There
is no `pm audit clear` command today.

## Contributor Surface

Emit through the public API:

```python
from pollypm.audit import emit
from pollypm.audit.log import EVENT_TASK_STATUS_CHANGED

emit(
    event=EVENT_TASK_STATUS_CHANGED,
    project=project_key,
    subject=f"{project_key}/{task_number}",
    actor=actor,
    status="ok",
    metadata={"from": old_state, "to": new_state, "reason": reason},
    project_path=project_root,
)
```

Rules for new events:

1. Add a stable `EVENT_*` constant in `src/pollypm/audit/log.py`.
2. Prefer `noun.verb` names and keep metadata JSON-serializable.
3. Emit from the mutation owner, not from a UI observer.
4. Keep the hook best-effort. Audit must not change the outcome of the
   underlying operation.
5. Add or update coverage in `tests/test_audit_log.py` for writer/reader
   behavior and `tests/test_audit_watchdog.py` when a detector consumes the
   event.
6. Update this document when the new event is operator-visible.

For watchdog work, keep pure detection logic in `pollypm.audit.watchdog` and
cadence/alert/dispatch wiring in
`pollypm.plugins_builtin.core_recurring.audit_watchdog`. This keeps detectors
testable without tmux, the job queue, or a live work-service database.
