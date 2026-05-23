# Automation Promotion — When Manual Tests Graduate to CI

Every manual test in this plan should eventually graduate to automated coverage — OR be explicitly retired as "not worth the bytes." This doc is the protocol for deciding.

---

## The promotion bar

**Question:** "If we removed this test, would we ship a regression in the next 6 months?"

- **Yes** → automate it.
- **No** → leave it manual or delete it.

The corollary: every manual test you run that finds NOTHING for 6 months in a row is a candidate to delete or downgrade. Don't accumulate dead checks.

---

## Promotion targets

### pytest (`tests/`)

For:
- Backend invariants (storage boundaries, audit-log schema).
- Task lifecycle state machine logic.
- API endpoint contracts (response shapes, status codes).
- State-cache consistency.
- Concurrency races (with deterministic injection).

Speed budget: full pytest suite < 10 minutes. Individual test < 5 seconds median.

### Playwright (`tests/playwright/`)

For:
- Web UI rendering and interaction.
- Cross-browser smoke (Chromium + mobile-chrome).
- 1-second click rule enforcement.
- End-to-end send-receive round-trip.
- Cookie / auth boundary in browser.

Speed budget: full Playwright sweep < 5 minutes with `--workers=4`.

### Custom perf harness

For:
- Latency budgets from §06.
- Resource consumption over time.
- Throughput / load tests.

Run the full M-scale harness pre-ship. Put lightweight, deterministic slices in CI:
- Bundle/payload size budgets.
- Critical Web click timings against fixture data.
- API microbenchmarks that do not depend on external network noise.

Do not hide behind "perf is noisy." Noisy measurements need better harness design, not no gate.

### Evals harness

For:
- Agent behavior quality (§04).
- Response category / structure assertions.
- Auth-marker handling.

Run on-demand pre-ship. Don't put in CI (slow + expensive token usage).

### Stays manual forever

Some checks genuinely can't be automated reliably:
- **Mobile UX on real phone hardware.** Emulator approximates but doesn't replace.
- **Magic-feel checks** (§03.9). "Did it anticipate what you needed?" requires human judgment.
- **Operator workflow end-to-end.** Same — judgment about whether the workflow is intuitive.
- **Cross-device sync verification** (laptop + phone simultaneously).

These run quarterly or pre-major-release. Document them clearly so they get run.

---

## Per-section promotion targets

### §00 Pre-flight Baseline

Already automated. Just runs the existing pytest + Playwright + `pm doctor`.

### §01 Task Lifecycle

| Scenario | Target | Notes |
|---|---|---|
| 1.1 Assignment paths | pytest integration | Use real PG. Test each path. |
| 1.2 State transitions | pytest | Drive REST API; assert PG state. |
| 1.3 Concurrency | pytest with threads | Verify single-winner outcomes. |
| 1.4 Visibility (TUI) | Textual pilot harness | Mount cockpit, assert glyphs. |
| 1.4 Visibility (Web) | Playwright | Assert state badges + dwell times. |
| 1.5 Cascade recovery | pytest integration | Drive failure, assert audit-trail. |

The audit-log trail assertions are the gold here. They prove the cascade fired the right sequence of events.

### §02 Translation Layer

All of §02 → pytest. Drive a known fixture transcript file, assert envelope shape exactly. The triple-witness consistency test (§2.6) is the most important.

### §03 Web UI Richness

| Scenario | Target |
|---|---|
| 3.1 First load | Playwright (cold paint timing) |
| 3.2 Surface enumeration | Playwright (compare TUI snapshot to Web snapshot) |
| 3.3 State indicators | Playwright (visual regression + assert badge content) |
| 3.4 Detail panels | Playwright |
| 3.5 1-second click rule | Playwright (per-interaction timing — **must be in CI**) |
| 3.6 Bidirectional sync | Playwright + tmux harness |
| 3.7 Daemon-down | Playwright + injected daemon kill |
| 3.8 Auth boundary | curl integration tests |
| 3.9 Magic-feel | **stays manual** |
| 3.10 Mobile-specific | mobile-chrome in Playwright + manual real-phone |

### §04 Agent Behavior

| Scenario | Target |
|---|---|
| 4.1 Canonical prompts | Evals harness |
| 4.2 Multi-turn coherence | Evals harness |
| 4.3 Auth-marker | **pytest** (deterministic + security-critical) |
| 4.4 Cross-agent context | Evals harness |
| 4.5 Refusal behavior | Evals harness |
| 4.6 Failure modes | Mostly evals; some manual |

### §05 Resilience & Recovery

| Scenario | Target |
|---|---|
| 5.1 `pm serve` kill/restart | Integration test |
| 5.2 Pane kill mid-task | pytest integration (mock tmux or real) |
| 5.3 DB drop | Integration test with toxiproxy |
| 5.4 Pause-marker | pytest + integration |
| 5.5 Token rotation | Playwright |
| 5.6 Network partition | Manual chaos test |
| 5.7 Resource exhaustion | Manual chaos test |

### §06 Performance Budgets

- **§6.1 scale matrix + §6.2 measurement rules** → harness contract; every perf result records environment + scale.
- **§6.3 user-facing budgets** → Playwright traces; critical click cells in CI, full M-scale pre-ship.
- **§6.4 API/data budgets** → perf harness; API microbenchmarks in CI where deterministic.
- **§6.5 polling/load** → pre-release load script; quarterly L-scale headroom.
- **§6.6 resource budgets** → scripted `make perf-snapshot` plus manual inspection until automated.
- **§6.7 end-to-end probes** → scriptable smoke/perf harness.
- **§6.8 soak** → manual monthly and before major release.
- **§6.9 issue template** → required for every `perf:` issue.

### §07 Quick Smoke

`make smoke` target. Most steps scriptable; console capture and real-phone ergonomics stay manual until browser automation covers them.

---

## Promotion workflow

When you finish a scenario manually and decide to promote:

1. **Write the automated test** as a separate commit before fixing anything.
2. **Verify the test fails** against current behavior (proves it catches what you found).
3. **Then fix** (separate commit).
4. **Verify the test passes** after the fix.
5. **PR includes both** — the regression test AND the fix.

This pattern is non-negotiable for §01 and §02 (the critical paths). Optional but strongly preferred for §03, §05.

---

## Promotion tracker

Use a markdown table in `docs/test-plan/promotion-status.md` (create as you go) to track:

| Scenario | Manual run date | Promoted? | Test path | Notes |
|---|---|---|---|---|
| 1.1.1 Worker picks queued | 2026-05-24 | ☐ | — | First manual pass found X |
| 1.1.2 Manual reassign | — | ✓ | tests/test_task_reassign.py | Already exists |

Run quarterly: review unpromoted manual checks. Decide: promote, retire, or accept-as-manual.

---

## The flake-fix policy (Fernanda)

When a promoted test starts failing intermittently:

1. **Don't ignore it.** A flaky test that's "just flaky" eats trust.
2. **Don't disable it casually.** Quarantine with a clear comment.
3. **Fix the root cause** within one sprint, OR
4. **Delete the test** if the underlying scenario doesn't justify the maintenance.

The bar: any test in CI must be reliable enough that a failure ALWAYS means "investigate." If it's "probably flake," it's worse than no test.

---

## When you finish the test plan

The output should include:
- All scenarios manually run with results.
- A list of `bug:` / `flake:` / `perf:` / `ux:` / `magic-gap:` issues filed.
- A promotion-status spreadsheet for next-sprint automation backlog.
- The 1-second-click-rule violations (separately flagged as ship-blockers).

That's the ship-readiness deliverable.
