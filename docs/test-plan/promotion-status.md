# Promotion Status

Use this tracker while executing the ship-readiness plan. Promote manual checks when they catch regressions that would plausibly ship without automation. Retire checks that never find signal.

**Source of truth:** journals are append-only and authoritative for a specific session's results. This file is a **rolling snapshot** — the latest meaningful result across sessions, plus the current promotion state. After each session, the tester walks their journal and pushes the relevant updates into the rows below. If this file and a journal disagree, the latest-dated journal wins. The tracker is regenerable from journals (manually).

**Result column legend:** `pass` / `fail` / `flake` / `partial` / `—` (not yet run).

**Promoted column legend:** `☐` (manual only) / `▶` (in progress) / `✓` (automated).

---

## §01 Task Lifecycle

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 1.1.1 Worker picks queued task | — | — | ☐ | — | pytest integration |
| 1.1.2 Manual reassign | — | — | ☐ | — | pytest |
| 1.1.3 Auto-claim sweep after pane kill | — | — | ☐ | — | integration + audit assertion |
| 1.1.4 Recovery dispatches after pause/resume | — | — | ☐ | — | integration |
| 1.2.1 Lifecycle happy path | — | — | ☐ | — | pytest |
| 1.2.2 Illegal transitions | — | — | ☐ | — | pytest |
| 1.2.3 Cancellation | — | — | ☐ | — | pytest |
| 1.2.4 Cancellation undo | — | — | ☐ | — | depends on whether `reopen` exists |
| 1.3.1 Concurrent claim race | — | — | ☐ | — | pytest with threads |
| 1.3.2 Concurrent project pause | — | — | ☐ | — | pytest with threads |
| 1.3.3 Concurrent inbox archive | — | — | ☐ | — | pytest with threads |
| 1.3.4 Concurrent doctor fix | — | — | ☐ | — | pytest with threads |
| 1.4 Visibility (TUI) | — | — | ☐ | — | Textual pilot harness |
| 1.4 Visibility (Web) | — | — | ☐ | — | Playwright |
| 1.4.4 Plan-review inbox handoff | — | — | ☐ | — | integration + audit |
| 1.4.5 "Why is this stuck?" | — | — | ☐ | — | Playwright + manual |
| 1.5 Cascade recovery | — | — | ☐ | — | pytest + audit-trail assertion |
| 1.6 Lifecycle perf at scale | — | — | ☐ | — | perf harness, M-scale release gate |

## §02 Translation Layer

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 2.1 Claude ingestor fidelity | — | — | ☐ | — | pytest |
| 2.2 Codex ingestor fidelity | — | — | ☐ | — | pytest |
| 2.3 REST recall correctness | — | — | ☐ | — | pytest |
| 2.4 REST injection | — | — | ☐ | — | pytest + Playwright |
| 2.5 Storage facade integrity | — | — | partial ▶ | tests/test_store_registry_sqlite_guard.py, tests/test_state_cache_no_sqlite_imports.py | extend with audit-schema test |
| 2.6 Triple-witness | — | — | ☐ | — | integration; the single most important promotion |
| 2.7 Translation perf under history | — | — | ☐ | — | perf harness with generated fixtures |

## §03 Web UI Richness

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 3.1 First load + cold paint | — | — | ☐ | — | Playwright |
| 3.2 Surface enumeration parity | — | — | ☐ | — | Playwright (compare TUI vs Web snapshots) |
| 3.3 State indicator parity | — | — | ☐ | — | Playwright visual regression |
| 3.4 Detail panel richness | — | — | ☐ | — | Playwright |
| 3.5 1-second click rule | — | — | ☐ | — | Playwright per-interaction timing — **must be in CI** |
| 3.5.1 Main-thread blocking | — | — | ☐ | — | Playwright trace inspection |
| 3.6 Bidirectional sync | — | — | ☐ | — | Playwright + tmux harness |
| 3.7 Daemon-down behavior | — | — | ☐ | — | Playwright + injected daemon kill |
| 3.8 Auth boundary | — | — | ☐ | — | curl integration + Playwright |
| 3.9.1 Magic-feel "I wish" log | — | — | manual forever | — | 30-min sit + journal capture |
| 3.9.2 TUI pilot parity | — | — | ☐ | — | Textual pilot harness |
| 3.10 Mobile-specific UX | — | — | ☐ + manual | — | mobile-chrome + real phone |

## §04 Agent Behavior

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 4.1 Canonical prompts per role | — | — | ☐ | — | Evals harness (lane H) |
| 4.1.3.1 Worker would-you-refuse | — | — | ☐ | — | Evals harness — security-critical |
| 4.2 Multi-turn coherence | — | — | ☐ | — | Evals harness |
| 4.3 Auth-marker handling | — | — | ☐ | — | **pytest** (deterministic + security-critical) |
| 4.4 Cross-agent context | — | — | ☐ | — | Evals harness |
| 4.5 Refusal behavior | — | — | ☐ | — | Evals harness |
| 4.6 Failure modes | — | — | mostly manual | — | drift / tool-loop / hallucination |
| 4.7 Agent perf + cost | — | — | ☐ | — | Evals harness metrics |

## §05 Resilience & Recovery

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 5.1 `pm serve` kill/restart | — | — | ☐ | — | integration test |
| 5.2 Pane kill mid-task | — | — | ☐ | — | pytest integration |
| 5.3 DB drop / reconnect | — | — | ☐ | — | toxiproxy-style integration |
| 5.4 Pause-marker enforcement | — | — | partial | tests/test_session_paused_marker_wiring.py | extend with §5.4 cells |
| 5.4.5 Malformed marker fail-closed | — | — | partial | tests/test_session_paused_marker_wiring.py | already covers basic case |
| 5.5.1 Bearer token rotation | — | — | ☐ | — | Playwright |
| 5.5.2 Claude subscription failover | — | — | ☐ | — | **release-gate scenario** — integration test against synthetic limit-reached |
| 5.5.3 Both subscriptions exhausted | — | — | ☐ | — | integration test — agents pause cleanly, task → blocked |
| 5.6 Network partition | — | — | manual quarterly | — | chaos |
| 5.7 Resource exhaustion | — | — | manual quarterly | — | chaos |
| 5.8 Data corruption recovery | — | — | ☐ | — | pytest (truncate, orphan, rotation) |
| 5.9 Recovery while under load | — | — | ☐ | — | pre-release harness |

## §06 Performance Budgets

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 6.1 Scale matrix + fixture seeds | — | — | ☐ | scripts/perf/seed_*.sh | lane E deliverable |
| 6.2 Measurement helpers | — | — | ☐ | scripts/perf/measure_http.sh | lane E deliverable |
| 6.3 User-facing budgets | — | — | ☐ | — | Playwright (critical cells), perf harness (full M-scale) |
| 6.4 API/data budgets | — | — | ☐ | — | perf harness; deterministic cells in CI |
| 6.5 Polling/load | — | — | ☐ | — | pre-release load script |
| 6.6 Resource budgets | — | — | ☐ | — | `make perf-snapshot` (lane E) + manual |
| 6.7 End-to-end probes | — | — | ☐ | — | scriptable smoke/perf harness |
| 6.8 Soak | — | — | manual monthly | — | quarterly L-scale headroom |

## §07 Quick Smoke

| Scenario | Manual Run | Result | Promoted? | Test Path | Notes |
|---|---|---|---|---|---|
| 7 Full 15-min smoke | — | — | ☐ | — | `make smoke` (lane F) |
| 7 5-min crunch version | — | — | ☐ | — | subset of `make smoke` |

---

## Quarterly cleanup

- Promote checks that repeatedly catch real regressions.
- Retire checks that never find signal AND are expensive to run.
- Keep manual-only checks only when human judgment or real hardware is essential.
- Update the **Result** column with the latest run; the column is not historical — keep the most recent meaningful result. Historical runs live in the journal.
