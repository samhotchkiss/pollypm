# 05 — Resilience & Recovery

**Goal:** prove the system survives realistic failure injection — process death, network partition, DB drop, pane kill — without losing state, hanging, or requiring manual operator intervention beyond clearly-surfaced prompts.

**Time:** 2–4 hours.

**Prereqs:** §00 green. §01 task lifecycle reliable (so we can tell when recovery actually works). §02 translation-layer reliable.

**Control-plane prerequisite for §5.2:** an isolated `pm serve` process is
not enough to exercise worker-spawn recovery. `pm serve` only hosts the
HTTP/Web API surface; it does not drain the heartbeat/job queue that runs
`task_assignment.sweep`, `session.health_sweep`, or the per-task worker
reclaim path. To mark §5.2 green, run against one of:

- a managed PollyPM home booted with `pm up`, with the rail daemon or cockpit
  HeartbeatRail alive and draining jobs;
- an explicit local scheduler/test dispatcher that constructs HeartbeatRail
  with workers enabled, calls `tick()`, and drains `task_assignment.sweep`.

If the run only has `pm serve` plus CLI/API calls, record §5.2 as
**unverified**, not passed. `pm heartbeat` by itself is also insufficient
unless a long-lived rail daemon/cockpit is present to drain the jobs it
enqueues.

**Important:** these tests are destructive. Run against a non-production PollyPM instance, or accept that you'll have to restart sessions. Have a known-good state snapshot before starting.

**Prereq: §00.0 environment safety check must have passed.** This section will:
- Kill the daemon.
- Drop PG connections.
- Restart processes mid-task.
- Corrupt the pause marker.
- Optionally fill disk.

If any of these would affect the operator's real workload, STOP. Move to a test environment.

Suggested targeted snapshot before starting (best-effort):
```bash
# Targeted snapshot — covers the destructive surfaces §05 actually touches.
# Do NOT use `cp -r ~/.pollypm` — on a busy box that tree can exceed 40 GB
# and take >10 minutes. The targeted backup below completes in <2 minutes.
#
# $SNAP is a per-invocation directory (mktemp -d) so reruns can't leak
# stale files from a prior §05 pass. Echo it so the operator can record
# the path in the engagement journal.

SNAP=$(mktemp -d -t pollypm-pre-05-snapshot-XXXX)
echo "$SNAP"

# PG state (the canonical source of truth)
pg_dump pollypm > "$SNAP/pollypm.sql"

# Session-pause marker (§05.3 corrupt-pause scenario)
cp ~/.pollypm/paused-sessions.json "$SNAP/" 2>/dev/null || true

# Audit trail (per-project .jsonl files, not the whole tree)
cp -r ~/.pollypm/audit/ "$SNAP/audit/" 2>/dev/null || true

# Briefings (referenced by recovery prompt tests)
cp -r ~/.pollypm/briefings/ "$SNAP/briefings/" 2>/dev/null || true

# Config
cp ~/.pollypm/pollypm.toml "$SNAP/" 2>/dev/null || true

# Source SHA for reproducibility
git -C /Users/sam/dev/pollypm rev-parse HEAD > "$SNAP/source.sha"
```

**If you want a clean, transactionally-consistent snapshot:** stop the daemon first, run the snapshot block above (which creates a fresh `$SNAP` directory via `mktemp -d`), then restart.

```bash
tmux send-keys -t pm-serve:serve C-c
sleep 5
# Re-run the targeted snapshot block above — `mktemp -d` will mint a new
# per-invocation $SNAP directory, so this variant does not collide with
# the live-daemon snapshot path.
tmux send-keys -t pm-serve:serve 'pm serve' Enter
```

Either approach is fine for a test environment. The targeted snapshot captures the surfaces §05 actually mutates; a full `cp -r ~/.pollypm` is not required and will time out on large workspaces.

Setup:
```bash
export BASE=http://$(tailscale ip -4):8765
export TOKEN=$(cat ~/.pollypm/api-token)
```

---

## What you need to know

### Recovery surfaces

- **`pm serve`** — the HTTP daemon. Killing it stops Web UI immediately.
- **`pm cockpit`** — the TUI. Independent process.
- **Per-session tmux panes** — each agent runs in its own pane.
- **PG** — the storage backend. PollyPM has a `connection_lost → reconnect` path; verify it works.
- **Tailscale** — auth + transport. UI breaks if Tailscale goes down.

### Heartbeat cascade

See §01 for the model. Key invariant: **manual fixes are bugs.** Anything an operator has to do by hand to recover from a failure should instead be encoded as a self-heal rule in the cascade.

---

## 5.1 `pm serve` kill/restart

### 5.1.1 Clean kill

Open Web UI. Verify everything looks normal. Then:
```bash
tmux send-keys -t pm-serve:serve C-c
```

**Expected within 30s:**
- Web UI badge: green → warn → error.
- Polling requests fail; UI shows clean error state.
- No white-screen, no traceback in the browser.

### 5.1.2 Restart

```bash
tmux send-keys -t pm-serve:serve 'pm serve' Enter
```

**Expected within 30s:**
- Next surface poll succeeds.
- Web UI badge: error → warn → green.
- Selected surface re-loads correctly (cached transcript may still show; new messages arrive).
- Cookie still valid (didn't lose session).

### 5.1.3 SIGKILL (uncleanly killed)

```bash
pkill -9 -f 'pm serve'
```

Restart. Same recovery as 5.1.2 — assert PollyPM doesn't accumulate stale state across restarts (e.g. stale `pm-serve.pid` files).

---

## 5.2 Pane kill mid-task

This is a **control-plane** test, not a Web API test. Before starting, verify
the scheduler is actually running:

```bash
pm doctor | grep 'rail-daemon-alive'
pm status --json | jq '.sessions[] | select(.kind == "per_task" or (.name | startswith("task-")))'
```

If no per-task workers appear after claiming a task, stop and fix the control
plane or switch to an explicit scheduler/test dispatcher. Do not substitute an
isolated `pm serve` run.

### 5.2.1 Worker pane killed

```bash
TID=$(pm task create --project pollypm "test-5-2-1" \
  --description "Exercise §5.2 worker pane recovery" \
  --json | jq -r .task_id)
pm task queue "$TID"
pm task claim "$TID" --actor worker
sleep 60  # let the per-task worker window boot and receive kickoff
pm task get "$TID"  # Assignee: worker..., Status: in_progress

PROJECT=$(printf '%s\n' "$TID" | cut -d/ -f1)
NUMBER=$(printf '%s\n' "$TID" | cut -d/ -f2)
tmux kill-window -t "pollypm-storage-closet:task-${PROJECT}-${NUMBER}"
```

**Expected cascade (within 3 min):**
1. Heartbeat / task-assignment sweep detects the missing per-task pane.
2. `_recover_dead_claims` releases the stale claim and records
   `task.reclaimed` / `worker_session_recovered`.
3. The auto-claim sweep re-claims `$TID`, provisions a fresh
   `task-${PROJECT}-${NUMBER}` window, and kickoff delivery resumes.

Verify via audit log:
```bash
grep -E 'heartbeat.missing|task.reclaimed|worker_session_recovered|worker_auto_claimed' ~/.pollypm/audit/pollypm.jsonl | tail -10
pm task get "$TID"
tmux list-windows -t pollypm-storage-closet | grep "task-${PROJECT}-${NUMBER}"
```

**Pass:** cascade fires. Task ends in a known state (`done`, `review`, `cancelled`, or queued again), never silently stuck.

### 5.2.2 Architect pane killed mid-thinking

Same pattern with architect. Important difference: architect's context may be long. Verify:
- Respawn loads from `events.jsonl` (architect history preserved).
- New session continues coherently (test by sending a follow-up that references prior context).

### 5.2.3 Cascade detection lag

Time the cascade:
- Pane killed at T=0.
- Heartbeat-missing detected at T=?
- Spawn initiated at T=?
- New session ready at T=?

**Budget:** total recovery (kill → ready) within 3 minutes. Slower means cascade isn't actively monitoring. File `perf:cascade-lag`.

---

## 5.3 DB connection drop

### 5.3.1 Restart PG mid-operation

```bash
# Identify your local PG instance
brew services restart postgresql@16  # or whatever your local PG is

# Or surgical:
psql -d pollypm -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='pollypm' AND pid != pg_backend_pid();"
```

Immediately try operations against the API:
```bash
curl -sS -H "Authorization: Bearer $TOKEN" $BASE/api/v1/dashboard
```

**Expected:**
- First call after disconnect: 5xx or 503 (acceptable, connection lost).
- Reconnect attempt within 5–10s.
- Subsequent call: 200.

**NOT acceptable:**
- Silent data loss (a task that was being claimed gets stuck).
- Double-claim after reconnect.
- Daemon crashes and doesn't restart.

### 5.3.2 PG paused (network unreachable)

Simulate transient PG outage:
```bash
brew services stop postgresql@16
sleep 30  # 30 seconds offline
brew services start postgresql@16
```

**Expected:**
- During outage: API returns 503 typed envelope, NOT 500 stack trace.
- After outage: recovery within 30s.
- No corrupt state.

---

## 5.4 Pause-marker enforcement

Per #2081 partial enforcement:
- Recovery loops (`no_session_spawn`, `Supervisor.maybe_recover_session`) **honor** the marker.
- Dispatch / cockpit / heartbeat loops **do NOT** honor it yet (#2068 tracks the rest).

### 5.4.1 Pause prevents recovery spawn

```bash
pm sessions pause worker_pollypm
# Confirm marker
cat ~/.pollypm/paused-sessions.json | jq

# Kill the worker pane
tmux kill-window -t pollypm:worker_pollypm

# Wait for recovery loop
sleep 180

# Verify: pane was NOT respawned
tmux list-windows -t pollypm | grep worker_pollypm
# Expect: no match (paused, not respawned)

# Audit should show pause.skip
grep "session.pause.skip" ~/.pollypm/audit/pollypm.jsonl | tail -3
# Expect: skip events for both no_session_spawn and supervisor.maybe_recover
```

### 5.4.2 Pause does NOT prevent dispatch (known partial)

Create a task while session is paused:
```bash
TID=$(pm task create --project pollypm "test-5-4-2" --json | jq -r .task_id)
pm task queue "$TID"
```

Per #2068 partial work, dispatch / cockpit may still attempt to send work. Verify behavior:
- Does it attempt? File `bug:pause-partial-incomplete` if so, but EXPECT this until #2068 fully closes.

### 5.4.3 Audit throttle

Trigger many pause-skip events in quick succession (multiple recovery cycles). Verify audit log doesn't get spammed:
```bash
grep "session.pause.skip" ~/.pollypm/audit/pollypm.jsonl | awk '{print $1}' | sort -u | wc -l
```

Per #2081 round-2, the audit emit is throttled (5 min per session+loop key). At most ~1 entry per session per loop per 5-min window.

### 5.4.4 Resume

```bash
pm sessions resume worker_pollypm
```

**Within 60s:** new session spawns, task gets picked up or remains intentionally paused with a visible reason.

### 5.4.5 Malformed marker fails closed

Per #2081 round-4:
```bash
printf "garbage\n" > ~/.pollypm/paused-sessions.json
```

Or even more aggressive:
```bash
chmod 000 ~/.pollypm/paused-sessions.json  # unreadable
```

**Expected:** `is_paused()` returns True for ALL sessions (fail-closed). No session gets recovered until operator fixes the marker.

Verify:
```bash
grep "session.pause.marker_unreadable" ~/.pollypm/audit/pollypm.jsonl | tail -1
# Should show a recent unreadable-marker diagnostic
```

`/api/v1/sessions/{name}/pause` should return 503 `marker_unreadable` instead of silently overwriting.

Restore:
```bash
chmod 644 ~/.pollypm/paused-sessions.json
echo "[]" > ~/.pollypm/paused-sessions.json
```

Within 60s of restore: marker_restored audit event.

---

## 5.5 Token rotation

### 5.5.1 Bearer token regen mid-session

UI is open and working. In another shell:
```bash
pm api regen-token
```

**Expected:**
- Next 5s poll: 401.
- UI shows auth error.
- Refresh `/ui/`: new cookie issued, UI recovers.

### 5.5.2 Claude subscription failover

**This is a release-gate scenario.** PollyPM's agents run on Claude subscriptions; the operator's primary subscription will hit its monthly limit, and the system must transition to a backup subscription without:
- Manual operator intervention.
- Dropped in-flight messages.
- A multi-minute outage.
- Confusing the architect / advisor / worker about who they are or what they were doing.

The operator's day-in-the-life assumes this works — if the primary hits its limit at 4pm and the architect goes dark for an hour, that's a failure mode the test plan must catch before ship.

**Setup (per §00.6):** confirm a backup Claude subscription is configured. If not, this scenario can't run and the failover behavior is unverifiable — file `magic-gap:no-failover-sub` and STOP.

**Trigger options (pick whichever PollyPM supports):**

```bash
# Option A — synthetic limit-reached
# If pollypm has a configuration knob to mark the primary sub as "over limit":
pm sub set-status primary --status over-limit
# (Replace with the actual CLI if it differs; check `pm --help` or src/pollypm/sub*.py)

# Option B — revoke the primary token mid-conversation
# Identify the primary token, regenerate it OR temporarily blacklist it via a non-destructive override
pm config set claude.primary.token "invalid-rotation-test"

# Option C — wait for an organic limit hit
# Only viable late in the month; rate-limit a session deliberately
```

After triggering, observe:

1. **Detection.** The system notices the primary is rejecting requests within one tool call / one assistant turn.
2. **Failover.** It switches to the backup subscription within ~30 seconds.
3. **Continuity.** An in-flight conversation (e.g., architect mid-planning) continues without operator intervention. The next assistant turn lands.
4. **Notification.** The operator sees a clear "failed over to backup" signal — in the UI, inbox, or audit log. NOT silent.
5. **Cost / quota visibility.** The operator can tell which subscription is now active and roughly how much headroom remains. Without this, they'll get surprised again when the backup also hits limit.

**Pass criteria:**
- Failover completes in < 60s from trigger.
- No `429 / 529` errors leak to the operator's UI.
- The architect / advisor / worker session does NOT restart or lose context.
- An audit event `account.failover.<primary→backup>` (or equivalent) lands in `~/.pollypm/audit/`.
- The Web UI shows the active account, not silently swapping it under the hood.

**Fail to file:**
- `bug:failover-silent` if failover happens but the operator can't tell.
- `bug:failover-context-loss` if the agent restarts / loses memory across the transition.
- `bug:failover-stuck` if requests keep hitting the primary after the limit.
- `magic-gap:failover-no-quota-headroom` if the backup is active but the operator has no way to see how much capacity it has.

**Restore after testing:**
```bash
pm sub set-status primary --status active  # or whatever resets the override
# Verify
pm sub list
```

### 5.5.3 Backup subscription also hits limit (rare but catastrophic)

What happens when BOTH the primary and backup are over limit? Trigger both. Expected behavior:
- Agents pause cleanly with a "no available subscription" message.
- The operator sees this immediately in their inbox.
- No in-flight task is lost; the task state becomes `blocked` with a clear reason.
- When either subscription is restored, agents resume from where they were.

**Pass:** graceful degradation, no data loss, clear operator messaging. **Fail:** silent hang, crashed sessions, or lost task state.

---

## 5.6 Network partition

### 5.6.1 Tailscale down

On the phone (or another tailnet peer): disable Tailscale.

**Expected on phone Web UI:**
- Polling fails (network unreachable).
- UI shows error state cleanly.
- Re-enabling Tailscale: UI recovers within 30s.

### 5.6.2 Mac Studio sleeps

If the daemon host (Mac Studio) sleeps:
- All tailnet peers lose access.
- On wake: daemon resumes (`pm serve` was running via tmux), UI peers reconnect.

Verify behavior on wake — sometimes Tailscale takes 10–30s to renegotiate.

---

## 5.7 Resource exhaustion

### 5.7.1 Disk full simulation

(Optional, destructive.) Fill `/tmp` to 100%. Verify:
- Audit-log writes fail gracefully (don't crash daemon).
- Operations return 5xx typed envelopes, not 500.
- Clear disk: operations resume.

### 5.7.2 PG connection pool exhausted

Run many concurrent API requests (e.g. 50 in parallel). PG connection pool size is bounded.

**Expected:** requests queue or 503 `pool_exhausted`. Never deadlock.

---

## 5.8 Data corruption recovery

**Goal:** PollyPM should survive a corrupted file or partially-written audit row without crashing or producing fictional state.

### 5.8.1 Truncated audit log

```bash
# Truncate a project's audit log mid-line
PROJECT_AUDIT=~/.pollypm/audit/pollypm.jsonl
cp $PROJECT_AUDIT /tmp/audit-backup
# Truncate to a random byte mid-record:
head -c $(($(wc -c < $PROJECT_AUDIT) - 50)) $PROJECT_AUDIT > /tmp/truncated
mv /tmp/truncated $PROJECT_AUDIT
```

Restart `pm serve` and observe:
- Does it start cleanly?
- Does `pm doctor` flag the corruption?
- Does the state cache refresh produce stale or fictional state?

**Pass:**
- Daemon starts.
- `pm doctor` reports `audit.corrupted` or similar alert.
- State cache reflects pre-corruption state without inventing rows.
- A subsequent valid audit append works.

**Fail (any of these):**
- Daemon crashes.
- Daemon refuses to start.
- Silent state divergence (state cache reads a half-record as a real event).

Restore: `mv /tmp/audit-backup $PROJECT_AUDIT`.

### 5.8.2 PG row partially deleted

Manually delete a task row that has live markers / messages referencing it:

```bash
psql -d pollypm -c "BEGIN; DELETE FROM tasks WHERE id = '<some_task_id>'; COMMIT;"
```

Try to read state:
- `pm task get <id>` should return a clean "not found," not 500.
- Web `/api/v1/tasks/.../<id>` should return 404 typed envelope, not 500.
- `pm cockpit` rail should not crash trying to render stale references.

**Pass:** referential integrity surfaces are clean. Orphan markers/messages either get GC'd by `pm doctor` or surface as alerts.

**Fail to file:** `bug:orphan-refs-crash` against the cockpit/route that crashed.

### 5.8.3 events.jsonl rotation mid-write

While a Claude session is actively writing, manually rotate the file:

```bash
# Simulate logrotate-style rotation
mv ~/.pollypm/sessions/pm-operator/events.jsonl ~/.pollypm/sessions/pm-operator/events.jsonl.1
touch ~/.pollypm/sessions/pm-operator/events.jsonl
```

The session continues writing. The new file gets new events. Verify:
- The translation layer (§02) can still read the current file.
- The rotated `.1` file is also readable via the API if pointed at it.
- No duplicate events surface.

**Pass:** new writes go to the new file; rotation doesn't break the API.

## 5.9 Recovery while under load

**Goal:** prove resilience mechanisms do not protect correctness by sacrificing all responsiveness.

At M-scale from §06, keep 3 desktop tabs and 1 phone open. Start the browser-equivalent poll load from §6.5.1, then inject one failure at a time:

| Failure | Performance budget during recovery |
|---|---:|
| Kill `pm serve`, restart | UI error visible within 30s; recovered clicks <1s after green |
| Kill worker pane mid-task | unrelated surface clicks remain <1s; cascade ready within 3 min |
| PG outage 30s | typed 503s only; recovery within 30s after PG returns |
| Token rotation | auth error visible by next poll; refresh recovers without long reload |

**Pass:** no failure injection creates a secondary performance collapse. The system may degrade, but it must degrade loudly, recover promptly, and return to §06 budgets without a manual cleanup.

File as `perf:recovery-load:<failure>` when correctness recovers but latency/resource use stays bad.

---

## 5.10 The "self-heal rule" audit (continued from §01)

For every failure mode in this section: write down what the operator had to do manually. **Every manual step is a bug.** The fix is in the cascade, not in operator runbooks.

Per memory: "Don't act as the heartbeat. When the recovery loop fails, fix the loop."

If after this section you have a list of "things I had to do manually," that's the self-heal sprint.

---

## Promotion to automation

- **5.1, 5.2, 5.4** → pytest integration tests; the cascade audit-log trail is the assertion.
- **5.3** → integration test with toxiproxy or similar to simulate PG drop.
- **5.5** → Playwright.
- **5.6, 5.7** → manual chaos tests, run quarterly.
- **5.8** → pre-release perf/resilience harness; not every PR.

---

## Out of scope

- Task state correctness (covered §01).
- Performance under load (covered §06).

---

## When you're done

Update test journal. Headline output:
- Manual-recovery list (each item = self-heal-rule sprint candidate).
- Cascade lag measurements.
- Resilience gaps (`bug:resilience-*`).
