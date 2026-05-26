# 01 — Task Lifecycle Integrity

**Goal:** prove tasks flow through the system reliably from creation to resolution, with full operator visibility at every step, and that the heartbeat recovery cascade catches broken tasks without human intervention.

**This is the heart of the product.** If §01 doesn't pass, nothing else matters.

**Time:** 4–6 hours.

**Prereqs:** §00 baseline green. `pm serve` running. At least one project tracked (`pollypm` itself works).

**User-surface rule:** the operator/user is never expected to run CLI commands. Command blocks in this file are tester setup, instrumentation, or failure injection. A user-facing lifecycle scenario only passes when the same state is observable and actionable through the product surfaces: Web UI via Playwright/real browser, and TUI via keystrokes sent to `pm cockpit` in tmux or a Textual `pilot` test.

Setup:
```bash
export BASE=http://$(tailscale ip -4):8765
export TOKEN=$(cat ~/.pollypm/api-token)
```

---

## What you need to know going in

### Task states

```
draft → queued → in_progress → review → done
                    ↘ rework ───────┘
                    ↘ blocked / on_hold
                    ↘ cancelled
```

Legal transitions only. Invalid lifecycle transitions must return typed errors (`409 invalid_state` for state conflicts, `422 validation_error` for unsupported transition surfaces). Do not treat a raw 500 as acceptable.

### Where state lives

- **PG**: `tasks` table (canonical), `markers` table (claim leases), `messages` table (notifications + inbox).
- **Audit**: `~/.pollypm/audit/<project>.jsonl` — per-project event log.
- **TUI**: `pm cockpit` reflects PG state via the state cache.
- **Web**: `GET /api/v1/dashboard` + `/api/v1/tasks/...` reflect PG state.

### Minimum-viable §01 (if time-constrained)

If you cannot run the full 4–6 hours, run these in order — they catch the highest-leverage failures:

1. **§1.5 cascade recovery** — if recovery doesn't self-heal, nothing else matters.
2. **§1.3 concurrency** — atomic claims and lost-update prevention are non-negotiable.
3. **§1.4 visibility** — if the operator can't see what's stuck, the system is unusable even when correct.
4. **§1.2.2 illegal transitions** — verifies error envelopes are typed, not 500s.

Skip 1.1.x happy paths last — they're the most likely to "just work" without targeted testing.

### Heartbeat cascade (tier model)

1. **Heartbeat (mechanical):** per-session "I'm alive" ping. Missed → escalate to PM.
2. **PM (project reasoning):** reads `audit.jsonl` + task state, decides what's broken, drafts a recovery brief.
3. **Polly (operator):** if PM can't self-resolve, surfaces to operator with actionable summary.

Manual patches without a self-heal rule are incomplete. If you find a failure mode the cascade should have caught, the fix is in the cascade, not in a manual claim/dispatch.

---

## 1.1 Assignment paths

**Goal:** every way a task gets assigned to an actor must work.

### 1.1.1 Worker picks queued task

Setup:
```bash
# Create a fresh queued task in the pollypm project
TASK_ID=$(pm task create --project pollypm "test-1-1-1" \
  --description "smoke worker pick" --json | jq -r .task_id)
pm task queue "$TASK_ID"
```

Verify it lands queued:
```bash
pm task get "$TASK_ID"
# Expect: Status: queued
```

Wait up to 60s for worker auto-claim sweep. Confirm:
```bash
pm task get "$TASK_ID"
# Expect: Status: in_progress, Assignee: worker...
```

**Pass criteria:**
- Functional: claim landed.
- Reliable: repeat 3x — every claim within 60s, every actor is a valid worker name.
- Fast: claim happens within 30s of creation (not 60s).
- Intuitive: `pm task get` makes it obvious who has it.

If claim never lands: heartbeat cascade should detect "unclaimed task ages out" and dispatch. If it doesn't, that's a heartbeat-cascade bug. File as `bug:heartbeat`.

### 1.1.2 Manual reassign

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"actor":"worker_pollypm/2"}' \
  "$BASE/api/v1/tasks/$TASK_ID/reassign" | jq '{ok,message,assignee:.task.assignee}'
pm task get "$TASK_ID"
# Expect: Assignee: worker_pollypm/2; context/audit breadcrumb shows the reassign
```

Reassign queued task should error (per #2064 contract):
```bash
QID=$(pm task create --project pollypm "test-1-1-2-queued" --json | jq -r .task_id)
pm task queue "$QID"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"actor":"worker_pollypm/1"}' \
  "$BASE/api/v1/tasks/$QID/reassign" | jq '{code,message,hint}'
# Expect: 409 invalid_state — claim first, or cancel/create a replacement
```

**Pass:** reassign succeeds on an in-progress task, errors clearly on queued.

### 1.1.3 Auto-claim sweep dispatches

Stop the worker pane: `tmux kill-window -t pollypm:worker_pollypm`.

Create a task. Wait up to 2 minutes. The recovery loop should:
1. Detect missing worker session.
2. Re-spawn it (`pm sessions launch worker_pollypm` semantics).
3. Worker picks the task.

If session never spawns: this is the `no_session_spawn` recovery loop failing. Check `~/.pollypm/audit/pollypm.jsonl` for `session.spawn.attempt` and `session.spawn.failed` events.

**Pass:** session respawns within 2 minutes; task claimed within 30s of respawn.

### 1.1.4 Recovery loop dispatches after pause/restart

```bash
pm sessions pause worker_pollypm  # per #2081 partial enforcement
# Confirm marker file:
cat ~/.pollypm/paused-sessions.json
# Should contain: worker_pollypm
```

Now create a task. Recovery loop must NOT spawn `worker_pollypm` (it's paused). Confirm via:
```bash
grep "session.pause.skip" ~/.pollypm/audit/pollypm.jsonl | tail -3
# Expect: skip events keyed to (worker_pollypm, no_session_spawn) and (worker_pollypm, supervisor.maybe_recover)
```

Resume:
```bash
pm sessions resume worker_pollypm
```

Within 60s, task should be claimed.

**Pass:** pause prevents spawn; resume re-enables. Skip events emit at most once per throttle window (5 min).

---

## 1.2 State transitions

**Goal:** every legal transition works; every illegal transition returns 409.

### 1.2.1 Happy path

Drive a task through the full lifecycle:
```bash
TID=$(pm task create --project pollypm "test-1-2-1" --json | jq -r .task_id)
pm task get "$TID"     # Status: draft
pm task queue "$TID"
pm task get "$TID"     # Status: queued
pm task claim "$TID" --actor worker_pollypm/1
pm task get "$TID"     # Status: in_progress
pm task done "$TID" --actor worker_pollypm/1 \
  --output '{"type":"code_change","summary":"test lifecycle output","artifacts":[]}'
pm task get "$TID"     # Usually Status: review on the standard flow
pm task approve "$TID" --actor reviewer --reason "test approval"
pm task get "$TID"     # Status: done
```

**Pass:** all transitions succeed; PG, REST, TUI, and Web state match at each step.

### 1.2.2 Illegal transitions

For each illegal transition, expect `409 invalid_state`:
- `draft → claim` (skipping queue)
- `queued → done` via `pm task done` (skipping claim)
- `done → in_progress` (going backwards)
- `cancelled → claimed`
- unsupported direct `PATCH status=in_progress` returns typed `422 validation_error`, not 500

```bash
# Example: skip claim
TID=$(pm task create --project pollypm "test-1-2-2" --json | jq -r .task_id)
pm task done "$TID" --actor worker_pollypm/1 \
  --output '{"type":"code_change","summary":"should not land","artifacts":[]}'
# Expect: 409 invalid_state with explanation
```

**Pass:** every illegal transition is rejected with a typed error envelope, no 500s.

### 1.2.3 Cancellation

```bash
TID=$(pm task create --project pollypm "test-1-2-3" --json | jq -r .task_id)
pm task cancel "$TID" --actor operator --reason "test cancel"
pm task get "$TID"  # Status: cancelled
# Worker should NOT pick it up
sleep 90
pm task get "$TID"  # still cancelled, assignee empty
```

**Pass:** cancelled tasks stay cancelled; no worker grabs them.

### 1.2.4 Cancellation undo (operator-error scenario)

Real operators cancel by mistake. Verify what happens when they want it back.

```bash
TID=$(pm task create --project pollypm "test-1-2-4" --json | jq -r .task_id)
pm task queue "$TID"
sleep 60  # let it claim
# Operator panics, cancels:
pm task cancel "$TID" --actor operator --reason "oops"

# Now try to recover:
pm task reopen "$TID" 2>&1 || echo "no reopen command"
# Or re-queue:
pm task queue "$TID" 2>&1 || echo "no requeue from cancelled"
```

**Three acceptable outcomes:**
- (a) A first-class `pm task reopen` (or `--undo`) restores the previous state.
- (b) Cancellation is a soft state; re-queueing from cancelled is allowed and surfaces a clear breadcrumb.
- (c) Cancellation is final; the operator must create a new task with a `Refs <cancelled task id>` link.

**Not acceptable:**
- (a) No way to recover AND no warning at cancel time.
- (b) `pm task reopen` exists but silently fails or leaves an inconsistent state.

If outcome (a) or (b), document the path. If only (c), file `magic-gap:cancel-no-undo` — operator panic-cancellation is common enough that "create a new task" friction is a real UX cost.

---

## 1.3 Concurrency

**Goal:** simultaneous mutations resolve correctly. No lost updates, no double-claims, no torn writes.

### 1.3.1 Simultaneous claims

```bash
TID=$(pm task create --project pollypm "test-1-3-1" --json | jq -r .task_id)
pm task queue "$TID"
sleep 2  # let it land

(curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
   -d '{"actor":"a"}' "$BASE/api/v1/tasks/$TID/claim" &
 curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
   -d '{"actor":"b"}' "$BASE/api/v1/tasks/$TID/claim" &
 wait)
```

**Expected:** exactly one returns 200, one returns 409. Verify breadcrumb names the winner.

```bash
pm task get "$TID"  # assignee is whoever won
pm task context $TID  # should show the loser's failed-claim breadcrumb
```

**Pass:** atomic; never both-200 or both-409.

### 1.3.2 Simultaneous project pause

```bash
(curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/v1/projects/booktalk/pause &
 curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/v1/projects/health_coach/pause &
 wait)
grep -A1 "key = 'booktalk'\|key = 'health_coach'" ~/.pollypm/pollypm.toml | grep tracked
```

**Expected:** both show `tracked = false`. Per `config_rmw_lock` — no lost update.

### 1.3.3 Simultaneous inbox archive

Find or create two open inbox items. Hit `archive` on both in parallel. Both should land. Now hit `archive` on the same item twice in parallel:

```bash
(curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/v1/inbox/pollypm/$ITEM/archive &
 curl -X POST -H "Authorization: Bearer $TOKEN" $BASE/api/v1/inbox/pollypm/$ITEM/archive &
 wait)
```

**Expected:** one 200, one 409 `invalid_state`.

### 1.3.4 Simultaneous `pm doctor fix=true`

```bash
(curl -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/doctor/run?fix=true" &
 curl -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/doctor/run?fix=true" &
 wait)
```

**Expected:** one 200, one 409 `in_progress` (single-flight per the doctor contract).
The 409 envelope includes `retry_after_seconds=5`.
Each API attempt should emit `pm.doctor_run` in `~/.pollypm/audit/_workspace.jsonl`,
including the rejected 409 path.

---

## 1.4 Visibility (operator-facing)

**Goal:** for every task state, the operator can immediately answer:
- WHERE is this task (which queue / who has it)?
- WHO has it (which worker)?
- HOW LONG has it been there?
- WHY is it stuck (if stuck)?

This is where the "kludgy, not magic" feeling lives. **Score on the intuitive + magical axes, not just functional.**

### 1.4.1 TUI visibility

Open `pm cockpit`. For a queued task: can you see it in the inbox? Right rail? Project pane?

For an in-progress task: same — where does it show up? Does the glyph match the state (◆ working, ◇ waiting, ▲ blocked)?

For a stuck task (no heartbeat for 5+ min): does the cockpit flag it? Where? With what affordance to act?

**Score honestly:**
- Found it in <5 seconds? Intuitive: pass.
- Had to scan multiple panes? Intuitive: fail. File `ux:visibility`.
- Couldn't find it at all without dropping to `pm task get`? Magical: fail. File `magic-gap:task-visibility`.

### 1.4.2 Web UI visibility

Open `/ui/`. Same questions. Can you see the task? Its state? Its actor? Time-in-state?

If the Web UI **doesn't expose tasks at all** (per known gap §326 in the old test plan), that's a documented limitation — file the issue for the next sprint, then evaluate inbox-item visibility as the proxy.

### 1.4.3 Time-in-state

For any task in any non-terminal state for >5 minutes, both TUI and Web should show **how long it's been there** without the operator doing math. Stamping "in_progress at 13:42" is not enough — show "working for 23m" or similar relative time.

**Pass:** every non-terminal task surfaces its dwell time at a glance.

### 1.4.4 Plan-review inbox handoff trace

The architect produces a plan; the operator gets a plan-review item in their inbox. Verify the mechanism, because everything downstream depends on it.

```bash
# Trigger: ask architect for a plan
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"Propose a plan for fixing X."}' \
  $BASE/api/v1/chat/architect_pollypm/send

# Wait for plan emission (varies; usually <2 min if architect has context)
sleep 60

# Verify the inbox item exists
pm inbox --json | jq '.tasks[] | select(.kind=="plan_review_pending")'
# Or via REST:
curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/inbox?type=plan_review&state=open" | jq

# Verify audit trail
grep -E '"event":"plan_review\.handoff_created"' ~/.pollypm/audit/pollypm.jsonl | tail -5
```

**Pass:**
- Plan-review item appears in inbox within 30s of architect emission.
- Inbox item references the source session (`architect_pollypm`) and the plan content.
- The inbox item metadata/labels include stable `handoff_id` and `correlation_id`
  values, and the audit row references the same IDs.
- Operator can act on it (approve, reject, comment) — see §3.4 for UI verification.

**If no inbox item appears:** the emit path is broken. Check `src/pollypm/work/plan_review_emit.py` for the emit site and the audit log for failures. File `bug:plan-review-emit`.

### 1.4.5 "Why is this stuck?"

Kill a worker pane mid-task: `tmux kill-window -t pollypm:worker_pollypm` (per §1.4.4 prereqs). The task is now stuck — nobody's working on it, but PG still says `in_progress`.

After 2 minutes (recovery cascade detection window), the operator should see:
- TUI: a clear "stuck" indicator on that task, with the reason (no heartbeat from actor).
- Web UI: same, surfaced in dashboard alerts.

If it's silent or requires drilling into `pm sessions --health` to discover, that's a magic-gap. File `magic-gap:stuck-task-surfacing`.

---

## 1.5 Heartbeat recovery cascade — the critical one

**Goal:** when a task breaks, the system fixes itself (or asks the operator clearly).

### 1.5.1 Worker crashed mid-task

```bash
# Get a worker working on a task
TID=$(pm task create --project pollypm "test-1-5-1" --json | jq -r .task_id)
pm task queue "$TID"
sleep 60  # let it claim
pm task get "$TID"  # confirm in_progress

# Kill the worker pane
tmux kill-window -t pollypm:worker_pollypm

# Wait for recovery cascade
sleep 180  # 3 min — recovery loop interval + buffer
```

**Expected:**
1. Heartbeat tier detects missing pane within ~60s.
2. Recovery loop (`auto_recover_no_session_alerts`) respawns `worker_pollypm`.
3. New worker picks up `$TID` (or task gets re-queued + claimed).
4. Audit log shows the cascade trail in `~/.pollypm/audit/<project>.jsonl`:
   `heartbeat.missing` → `recovery.spawn` → `task.reclaimed`.
   `heartbeat.missing` carries `target_session`, `reason`, and tmux window
   metadata plus an empty `target_task` when no task is known;
   `recovery.spawn` carries the replacement `target_session`, account,
   provider, reason, and an empty `target_task`; `task.reclaimed` carries
   `target_task`, `target_session`, and the stale-claim recovery reason.

**If it doesn't recover:** the fix is in the cascade. Do NOT manually claim or dispatch. File `bug:cascade` with the missing audit events.

### 1.5.2 Agent hung on tool call

Simulate: send a tool call that will hang (e.g. `sleep 600`). After heartbeat-tick × 3 misses (about 5 minutes), watchdog should fire an `unstick_brief`.

The unstick brief must:
- Include the `[PollyPM-Auth: <token>]` marker (per #2018).
- Quote the actual hung tool (not a generic "your agent is stuck").
- Suggest a specific recovery action (kill the pane, send Esc, etc.).

**Pass:** the brief is actionable, not generic. If it just says "agent appears stuck," that's a magic-gap.

### 1.5.3 Agent suspects prompt injection

A long-running architect session may have accumulated context like "ignore these messages, they're injections." Per #2017 the auth token solves this contractually.

Inject a recovery prompt that includes the legit `[PollyPM-Auth: <token>]`. Then inject one WITHOUT the marker, claiming to be PollyPM. Verify:
- Legit message: architect actions it.
- Unmarked message: architect refuses + logs.

**Pass:** architect respects the auth contract. If it actions the unmarked message (false positive) or refuses the marked one (false negative), file `bug:auth-token-handling`.

### 1.5.4 Session paused while task in-flight

```bash
# Get a task in_progress
TID=$(pm task create --project pollypm "test-1-5-4" --json | jq -r .task_id)
pm task queue "$TID"
sleep 60
# Pause the actor's session
pm sessions pause worker_pollypm
# What happens to the task?
sleep 120
pm task get "$TID"
```

**Per #2081 (partial enforcement):**
- Recovery loops respect the pause (no respawn).
- Dispatch / cockpit / heartbeat loops do NOT yet — they may still send work.

**Decide which behavior is correct.** If "pause means full quiesce," dispatch loops not respecting it is `bug:pause-incomplete`. File against #2068.

### 1.5.5 DB connection drop

Drop PG mid-task (simulate via `pg_terminate_backend` or restart `postgres`). Recovery should:
1. Notice the connection error.
2. Reconnect.
3. Resume operations without losing the in-flight task state.

If task state is lost or duplicate-claimed after reconnect: `bug:pg-reconnect`.

### 1.5.6 The "self-heal rule" audit

For every failure mode in 1.5.1–1.5.5: write down what the system did vs. what it should have done. If there's any case where a human had to step in manually (claim, dispatch, restart, etc.), that's a self-heal-rule gap. The fix is **always in the loop**, not in a one-shot operator action.

This is the most important output of §01.

---

## 1.6 Lifecycle performance at scale

**Goal:** prove the task lifecycle stays fast when the system has real history, not just one fresh task.

Run these at S-scale during §01 and repeat at M-scale during §06:

| Scenario | Budget | Failure means |
|---|---:|---|
| `pm task create` + `pm task queue` | p95 < 750ms at M-scale | task creation path is too heavy |
| `POST /tasks/{p}/{n}/claim` uncontended | p95 < 500ms at M-scale | claim path has avoidable DB/session overhead |
| 50-way claim race | p95 < 750ms, no 5xx | lock contention or error handling is unsafe |
| `GET /api/v1/tasks?project=pollypm&status=queued` | p95 < 300ms | list path scans too much state |
| TUI/Web visibility after task transition | visible within poll budget | state cache invalidation or polling is stale |

Also run a **queue storm**: create and queue 50 small tasks in one minute. While it runs, keep Web UI open and navigate surfaces.

**Pass:**
- UI clicks still meet the 1-second rule.
- Auto-claim/recovery loops do not starve dashboard polling.
- No duplicate worker sessions or duplicate claims.
- PG connections return to baseline within 60s.

If lifecycle correctness only passes when the system is empty, it is not release-ready.

---

## Promotion to automation

- **1.1, 1.2, 1.3** → pytest integration tests using real PG + faked actors. Build these first; they're the regression net.
- **1.4** → partly Playwright (UI state assertions), partly manual (operator UX). Manual cells should run on every UX-touching PR.
- **1.5** → pytest integration tests for the cascade logic. The audit-log trail assertions are gold — they prove the cascade fired the right sequence. Manual cells for 1.5.3 (auth-token UX).
- **1.6** → perf harness + Playwright timing; release gate via §06, not every PR.

Every test that catches a regression here is **higher value** than 10 unit tests elsewhere. This is the prime real estate.

---

## Out of scope

- UI styling — §03.
- Performance budgets — §06 (though 1-second click rule applies if you're testing visibility via UI).
- Network failure outside DB — §05.

---

## Common failure modes to expect

- **Stale state cache.** Task transitioned in PG but TUI/Web shows old state. Wait 15s (dashboard poll); if still wrong, state cache invalidation is broken. File against `state_cache/refresher.py`.
- **Audit event missing.** Cascade fired but the audit log doesn't show the steps. Means an emit site was bypassed — `bug:audit-coverage`.
- **Heartbeat false-negative.** Worker IS alive but heartbeat tier marks it dead. Probably a config / interval / clock-skew issue.
- **Heartbeat false-positive.** Worker is dead but heartbeat tier never escalates. Look for missed escalation thresholds in the cascade config.

## When you're done

Update your test journal with:
- Scenarios passed / failed / partial.
- Issues filed (`bug`, `flake`, `ux`, `magic-gap`).
- Self-heal-rule gaps (the §1.5.6 audit).
- Next section to run.
