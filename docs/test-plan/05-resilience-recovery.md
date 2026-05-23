# 05 — Resilience & Recovery

**Goal:** prove the system survives realistic failure injection — process death, network partition, DB drop, pane kill — without losing state, hanging, or requiring manual operator intervention beyond clearly-surfaced prompts.

**Time:** 2–4 hours.

**Prereqs:** §00 green. §01 task lifecycle reliable (so we can tell when recovery actually works). §02 translation-layer reliable.

**Important:** these tests are destructive. Run against a non-production PollyPM instance, or accept that you'll have to restart sessions. Have a known-good state snapshot before starting.

**Prereq: §00.0 environment safety check must have passed.** This section will:
- Kill the daemon.
- Drop PG connections.
- Restart processes mid-task.
- Corrupt the pause marker.
- Optionally fill disk.

If any of these would affect the operator's real workload, STOP. Move to a test environment.

Suggested snapshot before starting:
```bash
# State you can restore later
cp -r ~/.pollypm /tmp/pollypm-pre-05-snapshot
pg_dump pollypm > /tmp/pollypm-pre-05-snapshot.sql
git -C /Users/sam/dev/pollypm rev-parse HEAD > /tmp/pollypm-pre-05-snapshot.sha
```

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

### 5.2.1 Worker pane killed

```bash
TID=$(pm task create --project pollypm "test-5-2-1" --json | jq -r .task_id)
pm task queue "$TID"
sleep 60  # let worker claim
pm task get "$TID"  # Assignee: worker..., Status: in_progress

tmux kill-window -t pollypm:worker_pollypm
```

**Expected cascade (within 3 min):**
1. Heartbeat tier detects missing pane.
2. `no_session_spawn` recovery (per #2081 wiring) detects it, respawns `worker_pollypm`.
3. New worker session picks up `$TID` (or task is re-queued + claimed).

Verify via audit log:
```bash
grep -E 'heartbeat.missing|session.spawn|task.reclaim' ~/.pollypm/audit/pollypm.jsonl | tail -10
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

### 5.5.2 Cookie expiry approach

Cookie has 7-day Max-Age. There's no silent refresh — at expiry, the UI fails until refresh.

**Currently no banner.** This is a known gap. Verify by manually expiring the cookie (set client clock forward, or wait 7 days).

**Pass:** cookie expiry triggers a clean error state, not a crash. File `magic-gap:cookie-expiry-banner` for the UX improvement.

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
