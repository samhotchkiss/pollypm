# 06 — Performance Budgets

**Goal:** prove PollyPM is not merely functional, but consistently fast at realistic operator scale. Every number here is measured, repeatable, and tied to a ship/no-ship decision.

**Time:** 3–4 hours for the release gate, plus a 4+ hour soak. If anything fails, stop and fix before calling the product "ship-ready."

**Prereqs:** §00 green. `pm serve` running. UI accessible from desktop and phone. §01 and §02 reliable enough that performance failures are not hidden functional failures.

**Persona:** this section is Fernanda Raghavan's specialty (see `agent-personas.md`). Freya hands off to Fernanda for scale-matrix execution, p50/p95/p99/max measurement, and promotion decisions. "Feels faster" is rejected; numbers with environment + scale are required.

Setup:
```bash
export BASE=http://$(tailscale ip -4):8765
export TOKEN=$(cat ~/.pollypm/api-token)
```

---

## 6.0 Go/no-go rules

The release is **not shippable** if any of these are true:

- Any measured TUI or Web click takes **> 1,000ms** from input to visible response.
- Any critical endpoint has p95 above budget at **M-scale** (defined below).
- Any load run produces unexplained 5xx responses, deadlocks, duplicate task claims, or stale UI state lasting past the poll budget.
- `pm serve`, `pm cockpit`, browser memory, PG connections, or file descriptors grow continuously during the soak.
- Polling cost grows linearly with total transcript/task history when the UI only needs the current surface.
- The performance result cannot be reproduced because the environment, fixture size, or cache state was not recorded.

If a team wants to ship with one of these red, it requires explicit operator sign-off and a tracked `perf:` issue with owner + deadline.

---

## 6.1 Test scale matrix

Run budgets at **S** for daily smoke, **M** for release readiness, and **L** for headroom. M-scale is the promised-land bar.

### Fixture seeding

Each scale has a defined fixture. Codex lane E builds the seed scripts under `scripts/perf/seed_<scale>.sh`. They:
1. Create the configured number of sessions (operator, architects, advisors, workers).
2. Populate transcripts to the target message count via injected canned conversations.
3. Create tasks at the target count, spread across states (queued / in_progress / review / done).
4. Ensure one designated surface has the >1MB (M) or >10MB (L) transcript file.
5. Verify the resulting state matches the table below; exit non-zero on mismatch.

**Pre-§06 requirement:** before running §06, execute the appropriate seed script and verify with `pm sessions list --json | jq 'length'` and `pm task list --project pollypm --status all --json | jq 'length'`.

If `scripts/perf/seed_mscale.sh` does not exist, file `bug:perf-seed-missing` against lane E and either:
- Run §06 at S-scale only (smoke coverage), OR
- Manually seed to M-scale before continuing (document the procedure in your journal so the seed script can be reverse-engineered).

| Scale | Sessions | Tasks | Transcript history | Browsers | Purpose |
|---|---:|---:|---:|---:|---|
| **S — daily** | 4 active surfaces | 25 tasks | 50 messages/surface | 1 desktop | Quick regression signal |
| **M — release** | 20 active surfaces | 500 tasks | 5,000 messages total, one >1MB surface | 3 desktop tabs + 1 phone | Required ship gate |
| **L — headroom** | 50 active surfaces | 2,000 tasks | 25,000 messages total, one >10MB surface | 10 desktop tabs + 2 phones | Capacity confidence |

For every run, record:

```text
Date/time:
SHA:
Scale: S / M / L
Machine:
Browser + version:
Network path: loopback / tailnet / phone tailnet
Serve mode: tailnet trust / explicit-host
Sessions:
Tasks:
Largest events.jsonl:
Cache state: cold / warm
```

---

## 6.2 Measurement rules

**No stopwatches. No vibes. No single-sample wins.**

- Use at least **30 samples** for endpoint budgets. Report p50, p95, p99, max, status-code counts, and response size.
- Measure **cold** and **warm** paths separately. Cold means daemon restart or explicit cache clear; warm means consecutive calls against unchanged data.
- Treat p95 as the budget line. Treat max > hard limit as a ship-blocker for user interactions.
- Measure desktop and phone separately. Phone results must use real hardware for release readiness.
- Do not mix functional setup failures into perf numbers. If a request returns 4xx/5xx unexpectedly, fix or file it before computing percentiles.

### HTTP timing helper

The canonical implementation lives at **`scripts/perf/measure_http.sh`** (Codex lane E deliverable). The inline shell function below is its reference body, kept here so the doc is self-contained and §02 can reference the same logic.

```bash
measure_http() {
  local label="$1"
  local url="$2"
  local out="/tmp/pollypm-perf-${label}.tsv"
  : > "$out"
  for i in $(seq 1 30); do
    curl -sS -o /tmp/pollypm-perf-body \
      -w "${label}\t%{http_code}\t%{time_total}\t%{size_download}\n" \
      -H "Authorization: Bearer $TOKEN" \
      "$url" >> "$out"
  done
  python3 - "$out" <<'PY'
import math
import sys
from collections import Counter

rows = [line.rstrip("\n").split("\t") for line in open(sys.argv[1], encoding="utf-8")]
times = sorted(float(row[2]) for row in rows)
sizes = sorted(int(row[3]) for row in rows)
statuses = Counter(row[1] for row in rows)

def pct(values, p):
    return values[min(len(values) - 1, math.ceil(len(values) * p) - 1)]

print(
    f"samples={len(rows)} "
    f"p50={pct(times, 0.50):.3f} "
    f"p95={pct(times, 0.95):.3f} "
    f"p99={pct(times, 0.99):.3f} "
    f"max={times[-1]:.3f} "
    f"max_bytes={sizes[-1]}"
)
for status, count in sorted(statuses.items()):
    if not status.startswith("2"):
        print(f"bad_status {status} {count}")
PY
}

measure_http dashboard "$BASE/api/v1/dashboard"
```

When `scripts/perf/measure_http.sh` is available, invoke that instead of redefining. Drift between the script and this inline copy is a `bug:perf-helper-drift` against lane E. The Python runner behind the wrapper also provides named scenarios, JSON/Markdown reports, polling load, and resource snapshots; see `docs/test-plan/perf-harness.md`.

### Browser timing

Use Playwright traces or DevTools Performance. Measure from input event to the next paint that visibly completes the requested action.

Required browser metrics:

- First Contentful Paint (FCP)
- Largest Contentful Paint (LCP)
- Interaction to Next Paint (INP), or the closest manual trace equivalent
- Main-thread long tasks >200ms
- JS heap after 5 minutes idle
- Network response size for dashboard, sessions, and selected messages

Manual "felt under a second" notes are useful exploratory color, but they do **not** pass a release gate.

### TUI timing

Use Textual `pilot` or a terminal recording with timestamps. Manual counting is exploratory only. A TUI interaction passes only when measured input-to-render time is under budget.

---

## 6.3 User-facing budgets

These are the product feel budgets. A failure here blocks release even if backend endpoints are green.

| Interaction | S budget | M budget | Hard limit |
|---|---:|---:|---:|
| Web cold `/ui/` FCP | p95 < 1s | p95 < 1.5s | max < 3s |
| Web cold `/ui/` usable | p95 < 2s | p95 < 3s | max < 5s |
| Web warm reload usable | p95 < 500ms | p95 < 750ms | max < 1s |
| Surface click → transcript visible | p95 < 400ms | p95 < 750ms | max < 1s |
| Send click → UI acknowledgment | p95 < 400ms | p95 < 750ms | max < 1s |
| Dashboard refresh paint after response | p95 < 200ms | p95 < 300ms | max < 500ms |
| Switch surface during poll | p95 < 500ms | p95 < 750ms | max < 1s |
| Phone surface click → transcript visible | p95 < 750ms | p95 < 1s | max < 1s |
| TUI rail navigation | p95 < 150ms | p95 < 250ms | max < 1s |
| TUI pane mount first paint | p95 < 300ms | p95 < 500ms | max < 1s |
| Web INP | p95 < 200ms | p95 < 300ms | max < 500ms |

For every failed cell, capture a trace and file `perf:<surface>:<action>` with the trace path, scale, p95, max, and SHA.

---

## 6.4 API and data-path budgets

Run these at S and M. L-scale may use relaxed p95 budgets, but must not return unexplained 5xx or hang.

| Metric | S p95 | M p95 | Notes |
|---|---:|---:|---|
| `/api/v1/health` | < 50ms | < 50ms | No auth, no DB-heavy work |
| `/api/v1/dashboard` cold | < 200ms | < 300ms | Restart `pm serve` between cold samples |
| `/api/v1/dashboard` warm | < 75ms | < 150ms | Should not scale with full transcript history |
| `/api/v1/chat/sessions` | < 100ms | < 200ms | Includes configured + worker surfaces |
| `/api/v1/chat/{session}/messages?limit=50&direction=desc` cold, <1MB | < 75ms | < 100ms | Tail-read path |
| Same endpoint warm | < 15ms | < 25ms | Cache hit |
| Same endpoint cold, >10MB transcript | < 150ms | < 250ms | Verifies no full parse for small desc limit |
| `direction=asc&limit=100` on long transcript | < 300ms | < 500ms | Forward path allowed to be slower |
| `include_thinking=true` | < 150ms | < 250ms | No cache pollution afterward |
| `include_subagents=true` | < 500ms | < 750ms | Also assert payload budget |
| `/api/v1/chat/{session}/send` response | < 300ms | < 500ms | API response only |
| Send-to-pane end-to-end | < 750ms | < 1s | Timestamp script from §6.7 |
| Pane-to-UI end-to-end | < 6s | < 6s | 5s poll + 1s render |
| `/api/v1/tasks` filtered list | < 150ms | < 300ms | Filter by project/status |
| `/api/v1/tasks/{p}/{n}` detail | < 100ms | < 200ms | Includes plan/details |
| `/api/v1/tasks/{p}/{n}/claim` | < 200ms | < 500ms | Under no contention |
| Concurrent claim race | < 500ms | < 750ms | Exactly one 200, rest typed 409/429 |
| `/api/v1/inbox` | < 200ms | < 400ms | Include default actionable view |
| `pm doctor` clean | < 5s | < 8s | CLI wall time |
| `pm task create` + `pm task queue` | < 500ms | < 750ms | CLI wall time |
| `pm task get` | < 100ms | < 200ms | CLI wall time |

Payload budgets:

| Payload | Budget |
|---|---:|
| `/ui/` HTML | < 8KB |
| `/ui/app.js` | < 40KB uncompressed |
| `/ui/styles.css` | < 40KB uncompressed |
| `/api/v1/dashboard` at M-scale | < 75KB |
| `/api/v1/chat/sessions` at M-scale | < 100KB |
| 50-message response | < 150KB unless `include_subagents=true` |
| `include_subagents=true` response | < 750KB |

If a response exceeds budget, the fix is usually pagination, summarization, lazy loading, or excluding fields by default — not raising the budget.

---

## 6.5 Polling and multi-client load

The Web UI polls. That means product performance depends on aggregate background cost, not just single-click latency.

### 6.5.1 Browser-equivalent poll load

Run for 5 minutes at M-scale:

```bash
for client in $(seq 1 10); do
  (
    end=$((SECONDS + 300))
    while [ $SECONDS -lt $end ]; do
      curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/dashboard" > /dev/null &
      curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/chat/sessions" > /dev/null &
      curl -sS -H "Authorization: Bearer $TOKEN" "$BASE/api/v1/chat/operator/messages?limit=50&direction=desc" > /dev/null &
      wait
      sleep 5
    done
  ) &
done
wait
```

Canonical runner equivalent:

```bash
python3 scripts/perf/measure_http.py poll \
  --base "$BASE" \
  --clients 10 \
  --duration 300 \
  --interval 5 \
  --json-out /tmp/pollypm-poll.json \
  --markdown-out /tmp/pollypm-poll.md
```

**Pass:**
- No unexplained 5xx.
- Endpoint p95s still meet M-scale budgets.
- `pm serve` CPU does not stay above 50%.
- PG connections return to baseline within 60s after the run.
- UI remains interactive during the load.

Resource snapshot:

```bash
python3 scripts/perf/measure_http.py resources \
  --json-out /tmp/pollypm-resources.json \
  --markdown-out /tmp/pollypm-resources.md
```

### 6.5.2 Concurrent reads

```bash
seq 1 100 | xargs -P20 -I{} \
  curl -sS -o /dev/null -w "%{http_code} %{time_total}\n" \
  -H "Authorization: Bearer $TOKEN" \
  "$BASE/api/v1/dashboard"
```

**Pass:** all 2xx, p95 < 1s, no deadlock, no stuck PG connections.

### 6.5.3 Concurrent writes

Create one queued task, then race 50 claim requests:

```bash
TASK_ID=$(pm task create --project pollypm "perf-claim-race" --json | jq -r .task_id)
pm task queue "$TASK_ID"
seq 1 50 | xargs -P20 -I{} \
  curl -sS -o /tmp/claim-{} -w "%{http_code} %{time_total}\n" \
  -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"actor":"load-test-{}"}' \
  "$BASE/api/v1/tasks/$TASK_ID/claim"
```

**Pass:** exactly one success or one rollback-to-queued response, all other failures are typed 409/422/429, no 5xx, p95 < 750ms.

### 6.5.4 Queue storm

Create and queue 50 small tasks in one minute. Verify:
- The UI remains responsive.
- Auto-claim/recovery loops do not starve dashboard polling.
- `pm task list --project pollypm --status queued` and `/api/v1/tasks?project=pollypm&status=queued` stay within budget.
- No duplicate worker session names.

---

## 6.6 Resource budgets

Capture before, during, and after S/M/L runs.

| Resource | Idle budget | M-load budget | Leak rule |
|---|---:|---:|---|
| `pm serve` RSS | < 200MB | < 350MB | < 5% growth/hour after warmup |
| `pm serve` CPU idle | < 1% avg | < 50% avg during load | Returns to < 5% within 60s |
| `pm cockpit` RSS | < 300MB | < 450MB | < 5% growth/hour |
| `pm cockpit` CPU idle | < 2% avg | < 25% avg during nav | Returns to idle |
| Browser tab JS heap | < 50MB | < 100MB | No steady growth across 30 min |
| PG connections | < 20 normal | < 50 under load | Returns to baseline within 60s |
| Open file descriptors, `pm serve` | < 256 | < 512 | No monotonic growth |

Useful probes:

```bash
pgrep -f 'pm serve' | head -1 | xargs -I{} ps -o pid,rss,%cpu,command -p {}
pgrep -f 'pm cockpit' | head -1 | xargs -I{} ps -o pid,rss,%cpu,command -p {}
lsof -p "$(pgrep -f 'pm serve' | head -1)" | wc -l
psql -d pollypm -c "SELECT count(*) FROM pg_stat_activity WHERE datname='pollypm';"
```

---

## 6.7 End-to-end latency probes

### 6.7.1 Send-to-pane

```bash
SEND_TS=$(date +%s.%N)
MSG="perf-test-$SEND_TS"
curl -sS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"text\":\"$MSG\"}" "$BASE/api/v1/chat/operator/send" > /dev/null

while ! tmux capture-pane -t pollypm:pm-operator -p | grep -q "$MSG"; do
  sleep 0.05
done
ARRIVE_TS=$(date +%s.%N)
echo "scale=3; $ARRIVE_TS - $SEND_TS" | bc
```

**Budget:** p95 < 1s at M-scale.

### 6.7.2 Pane-to-UI

Send a unique message from the tmux pane, then measure when it appears in the browser trace. Use the `events.jsonl` timestamp to split:

- Pane → ingestor/archive latency
- Archive → API response latency
- API response → Web paint latency

If the total is >6s, identify which segment owns the delay. Do not file one generic "UI slow" issue.

---

## 6.8 Long-running soak

Run for 4+ hours at M-scale with normal operator activity and at least one phone open.

Every 30 minutes record:
- `pm serve` RSS/CPU/fd count
- `pm cockpit` RSS/CPU
- PG connection count
- `/api/v1/dashboard` p95 from 10 samples
- Browser heap
- `pm doctor`
- `pm sessions health`

**Pass:** no monotonic resource growth, no new stale-heartbeat cluster, no connection-pool exhaustion, no progressive UI slowdown.

---

## 6.9 Perf issue template

Every `perf:` issue includes:

```text
Title: perf(<surface>): <action> exceeds <budget>
SHA:
Scale:
Environment:
Metric:
Budget:
Observed p50/p95/p99/max:
Status-code counts:
Payload size:
Trace/log paths:
Repro steps:
Likely bottleneck:
User impact:
Ship-blocker: yes/no
```

Developers appreciate this. "Feels slow" is a complaint; this is a bug report.

---

## Promotion to automation

- **Critical click budgets** → Playwright CI on every PR touching Web UI.
- **HTTP budget table** → `scripts/perf/measure_http` or equivalent; run pre-ship and on perf-sensitive PRs.
- **Payload budgets** → CI check on every PR touching Web API or UI.
- **Polling load** → scheduled or pre-release load script; not every PR.
- **Resource soak** → manual pre-major-release; automate sampling output when possible.
- **Regression probes** → every perf fix adds a targeted test or benchmark that would have caught the regression.

---

## Out of scope

- Functional correctness — §01, §02.
- UX clarity beyond response time — §03.
- Agent answer quality — §04.
- Failure injection correctness — §05.

---

## When you're done

Update the test journal with:
- Completed S/M/L matrix and environment.
- Budget table with p50/p95/p99/max, not just pass/fail.
- All `perf:` issues filed with traces.
- Explicit ship/no-ship recommendation.
- Any budget you believe is unrealistic, with evidence and a proposed replacement.
