# Journal Template — Test Session Record

**Copy this file to `docs/test-plan/journals/<YYYY-MM-DD>-<short-label>.md` at the start of every test session. Without a journal, the session didn't happen.**

The journal is the durable record of a test pass — what was tested, what failed, what was filed, and the ship/no-ship decision. It outlasts conversation context, allows a new operator to pick up where you stopped, and forms the basis for the next sprint's backlog.

---

## Template (copy below this line)

```markdown
# Test Session — <YYYY-MM-DD> — <short label, e.g. "ship-readiness-rc1">

## Header

- **Persona:** <Freya Haugen / other testing persona from agent-personas.md>
- **Session start:** <YYYY-MM-DD HH:MM TZ>
- **Tested SHA:** <git rev-parse HEAD at start>
- **Environment:**
  - Machine: <model / CPU / RAM>
  - OS: <darwin x.y / linux z.z>
  - Browser(s): <Chrome version, Safari version, etc.>
  - Phone: <model + OS version, or "N/A">
  - Network path: <loopback / tailnet desktop / tailnet phone>
  - Serve mode: <tailnet trust / explicit-host>
  - Test env marker: <present / absent>
- **Fixture scale at start:** <S / M / L / ad-hoc, with: N sessions, M tasks, largest events.jsonl bytes>
- **Models (per §00.6):**
  - Operator: <model name + version>
  - Architect: <model name + version>
  - Advisor: <model name + version>
  - Worker: <model name + version>
- **Time budget:** <2h / 8h / 24h / open-ended>

## Baseline (§00 result)

- pytest: <X passed / Y failed — list failures or "only known">
- Playwright: <X passed / Y failed — list failures or "all pass">
- pm doctor: <clean / N alerts>
- pm sessions --health: <clean / N stale>
- Baseline verdict: <green / yellow / red>

If baseline is red, stop here and either fix or document why proceeding anyway.

## Scenarios run

For each section/scenario:

### §<X.Y> <Scenario name>

- **Result:** pass / fail / flake / partial / skipped
- **Axes:** functional / reliable / fast / intuitive / magical — note per-axis
- **Evidence:** <command output snippet, screenshot path, trace path, perf numbers, audit-log excerpt>
- **User-surface evidence:** <Playwright trace / browser screenshot / tmux keystroke transcript / Textual pilot output, or "not user-facing">
- **Issues filed:** #<N> <bug:|flake:|perf:|ux:|magic-gap:><short>
- **Notes:** <observations, follow-up threads, what was interesting>

Repeat for every scenario.

## Performance results (§06)

Only fill in if §06 ran. Capture the §6.1 scale and the §6.3 / §6.4 budget table cells.

- **Scale tested:** S / M / L
- **Fixture detail:** <N sessions, M tasks, biggest events.jsonl bytes>

### §6.3 user-facing budgets

| Interaction | Budget (p95) | Observed p50 | p95 | p99 | max | Pass? |
|---|---:|---:|---:|---:|---:|---|
| Web cold `/ui/` FCP | < 1.5s | — | — | — | — | — |
| Surface click → transcript | < 750ms | — | — | — | — | — |
| Send → UI ack | < 750ms | — | — | — | — | — |
| (… fill all rows from §6.3 table …) | | | | | | |

### §6.4 API/data budgets

| Metric | M budget | p50 | p95 | p99 | max | Pass? |
|---|---:|---:|---:|---:|---:|---|
| `/api/v1/health` | < 50ms | — | — | — | — | — |
| `/api/v1/dashboard` warm | < 150ms | — | — | — | — | — |
| `/messages?limit=50&direction=desc` cold | < 100ms | — | — | — | — | — |
| (… fill all rows from §6.4 table …) | | | | | | |

### §6.5 / §6.6 load + resource

- Concurrent reads (100x): <all 2xx / N 5xx>; p95 = <ms>.
- Concurrent claims (50x): <one 200 / rest 409 expected / actual>; p95 = <ms>.
- `pm serve` RSS idle → peak load → settled: <MB / MB / MB>.
- PG connections at peak: <N>; returned to baseline: <Y/N within 60s>.
- Soak result (if run): <duration; resource trend, alerts, p95 drift>.

## Issues filed

| # | Severity | Title | Owner label | Ship-blocker? |
|---|---|---|---|---|
| #<N> | bug | <short> | needs-codex / needs-claude | yes / no |
| #<N> | perf | <short> | needs-codex / needs-claude | yes / no |
| #<N> | magic-gap | <short> | needs-codex / needs-claude | yes / no |

## Self-heal-rule gaps (§1.5.6, §5.10)

Each item: failure mode + what the operator had to do manually + which loop should encode the rule.

1. <failure mode> → operator <action> → rule belongs in <module/loop>.
2. …

## Promotion-status changes

- Promoted: <scenario> → <test path>.
- Retired: <scenario> — no signal in N runs.

(Mirror these into `promotion-status.md` after the session.)

## Ship / no-ship recommendation

- **Recommendation:** ship / yellow-with-caveats / do-not-ship
- **Reasoning:** <1-2 paragraphs tying scenarios + issues to the criteria in README.md::Ship / no-ship decision>
- **Blocking issues:** #<N>, #<N>, …
- **Yellow caveats (if applicable):** <list of accepted-risk issues with owner + deadline>

## Operator decisions taken during session

For any time the test paused to ask the operator:

- <timestamp> — paused for: <question>. Operator decided: <decision>. Rationale: <reason>.

## Followups / open threads

- <Thread> — <next step / who picks it up>.
- <Wish item from §3.9.2 "I wish I could…"> — <severity>.

## Session end

- **Session end:** <YYYY-MM-DD HH:MM TZ>
- **Wall-clock duration:** <hours>
- **Active testing duration:** <hours; excludes fix-loop waits>
- **Next session entry point:** <which section / scenario picks up next time>
```

---

## Naming convention

- `journals/2026-05-24-ship-readiness-rc1.md` — full plan run.
- `journals/2026-05-24-smoke.md` — quick smoke only.
- `journals/2026-05-24-cascade-recheck.md` — targeted scenario re-run after a fix.

One file per session. Don't append to a prior journal — start a new file and link back.

## What NOT to do in the journal

- Don't write up your reasoning before the test runs. The journal records what happened, not what you predicted.
- Don't strip detail to "make it readable." A useful journal has command output, trace paths, and audit excerpts. Future-you will thank present-you.
- Don't claim a scenario passed without recording at least one observable assertion. "Looks good" is not a result.
- Don't conflate axes. A scenario that's functionally correct but slow gets two entries — `pass (functional)` and `fail (fast)` — not "mostly pass."

## Discovery

`ls docs/test-plan/journals/ | sort -r | head -5` — see most recent sessions.

`grep -l "ship-readiness" docs/test-plan/journals/*.md` — find all full-plan runs.
