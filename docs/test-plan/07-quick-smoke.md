# 07 — Quick Smoke (15-Min Daily Driver)

**Goal:** a fast pass that verifies main is shippable right now. Designed to run any time — before a merge, after a deploy, when something feels off, every morning during ship-readiness.

**Time:** 15 minutes.

**Prereqs:** none. This is the lightest-weight check.

---

## What this catches vs. doesn't

**Catches:**
- Daemon crashes.
- API auth/transport breakage.
- Web UI white-screen / console errors.
- Task lifecycle broken end-to-end.
- Send-to-pane / pane-to-UI sync broken.
- `pm doctor` regressions.

**Doesn't catch:**
- Subtle drift (use §02).
- Agent quality issues (use §04).
- Concurrency races (use §01.3).
- Resource leaks (use §06.2 + §06.6).
- M-scale performance regressions (use §06 before ship/no-ship).

If §07 is green, you can ship a small change. If §07 is red, **do not ship** — investigate.

---

## The checklist

Open this file. Run each step. Check the box. Anything red → stop and fix.

### Setup (2 min)

```bash
cd /Users/sam/dev/pollypm
git rev-parse HEAD                           # record SHA you're testing
export BASE=http://$(tailscale ip -4):8765
export TOKEN=$(cat ~/.pollypm/api-token)
```

- ☐ Working directory is on `main`, no uncommitted changes (unless that's the thing under test).
- ☐ `pm serve` is running (`tmux capture-pane -t pm-serve:serve -p | head -2` shows the bind line).
- ☐ `BASE` and `TOKEN` set.

### REST liveness (1 min)

```bash
curl -sS $BASE/api/v1/health
curl -sS -o /dev/null -w "dashboard %{http_code} %{time_total}s\n" -H "Authorization: Bearer $TOKEN" $BASE/api/v1/dashboard
curl -sS -o /dev/null -w "sessions %{http_code} %{time_total}s\n" -H "Authorization: Bearer $TOKEN" $BASE/api/v1/chat/sessions
curl -sS -H "Authorization: Bearer $TOKEN" $BASE/api/v1/dashboard | jq '.daemon_status'
curl -sS -H "Authorization: Bearer $TOKEN" $BASE/api/v1/chat/sessions | jq '.sessions | length'
```

- ☐ `/health` returns `{"status":"ok",...}` 200.
- ☐ `/dashboard` returns JSON with `daemon_status` present, 200.
- ☐ `/chat/sessions` returns a list of >0 sessions.
- ☐ Single-shot `/dashboard` and `/chat/sessions` are both comfortably under 1s. If not, run §06.

### Web UI (3 min)

Open `http://<tailnet-ip>:8765/ui/` in a fresh tab.

- ☐ Page loads within 3 seconds (cold paint).
- ☐ Header + left rail + right rail + center pane all render.
- ☐ Left rail shows session list (≥1).
- ☐ Right rail shows 5 rollup cards.
- ☐ Devtools → Console: zero errors.
- ☐ Devtools → Network: no 4xx/5xx.

### Surface click (1 min, **enforces 1-sec click rule**)

- ☐ Click `operator` in left rail → transcript loads in <1s.
- ☐ Click `architect_pollypm` → switches in <1s.
- ☐ Click any worker → loads in <1s (or shows "no messages yet" cleanly).

**If any click breaks 1 second: STOP. This is a ship-blocker. File `perf:click-latency`.**

### Send-receive round-trip (2 min)

In Web UI:
- ☐ Select `operator`. Type "smoke test $(date +%H%M%S)". Send.
- ☐ Within 1 second: appears in `tmux capture-pane -t pollypm:pm-operator -p | tail -5`.

In tmux operator pane:
- ☐ Type "echo from tmux $(date +%H%M%S)" and Enter.
- ☐ Within 5 seconds: appears in Web UI message list.

### Task cycle (3 min)

```bash
TID=$(pm task create --project pollypm "smoke-$(date +%H%M%S)" --json | jq -r .task_id)
pm task queue "$TID"
sleep 60
pm task get "$TID"
```

- ☐ Task created successfully (got an ID).
- ☐ Within 60s: `pm task get` shows Status=`in_progress`, Assignee=worker.

If your worker actually processes tasks, optionally:
```bash
sleep 60
pm task get "$TID"
```

- ☐ State progresses to `review` / `done` (worker did the work) or stays `in_progress` (long task — acceptable).

### Doctor (1 min)

```bash
pm doctor
```

- ☐ Exits 0 with no `[FAIL]` lines.
- ☐ Alerts (if any) are clustered when 3+ share `alert_type` (per #2038).

### Sessions health (1 min)

```bash
pm sessions health
```

- ☐ Lists active sessions.
- ☐ No `stuck` heartbeats from sessions started before this smoke run.

### Daemon resilience (1 min, optional)

If you have time / it's a deeper check:

```bash
tmux send-keys -t pm-serve:serve C-c
sleep 30
# Web UI badge should be red
tmux send-keys -t pm-serve:serve 'pm serve' Enter
sleep 30
# Web UI badge should be green again
```

- ☐ UI badge flips to error within 30s of kill.
- ☐ UI badge recovers to green within 30s of restart.

---

## Result

| Status | Action |
|---|---|
| **All green** | Shippable. Note the SHA + time. |
| **Any red** | **Do not ship.** Investigate. If the red is a known issue with a tracked PR, you can ship if explicitly approved by operator. Otherwise fix first. |

Record in journal:
```
Smoke pass: 2026-MM-DD HH:MM
SHA: <sha>
Result: all green / red on <check>
```

---

## When to run this

- Every morning during ship-readiness sprint.
- Before any merge to main.
- After any significant infrastructure change (PG migration, Tailscale config, daemon restart).
- When something "feels off" — runs in 15 min, catches the obvious.
- After running §00–§06 — final confirmation.

---

## Promotion

Most of this CAN be automated as a `make smoke` target. Worth doing — it'd run in under 5 minutes scripted.

What stays manual:
- "Devtools console zero errors" until Playwright console capture is wired.
- Real-phone feel and ergonomics.

The rest is scriptable.
