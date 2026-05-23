# Parallel Execution Plan

**Goal:** multi-thread ship-readiness without agents stepping on each other. Claude owns deep validation and judgment-heavy investigation. Codex owns product code creation where contracts are stable enough to build against.

Use `agent-personas.md` for exact testing, coding, and reviewing personas. Lane owners below are operational assignments; persona rules define how each agent behaves.

---

## Ground rules

- One branch per workstream.
- One owner per file family.
- No backend API contract changes from UI threads without posting the contract gap first.
- Every code-producing thread ships with targeted tests or a documented manual verification path.
- Performance is a first-class deliverable, not a follow-up.
- Issues and PRs use exactly one ownership label: `needs-codex` or `needs-claude`.
- The authoring agent never approves or merges its own PR; the other agent reviews, approves, and merges.
- All code changes must preserve `architecture-guardrails.md`: modular boundaries, plugin ownership, thin routes, public API contracts, and storage facades.

If two agents need the same files, split by sequence: one writes, the other reviews after merge/rebase. Do not co-edit.

---

## Critical path split

| Lane | Owner | Primary scope | Branch | File ownership |
|---|---|---|---|---|
| A | Claude | §00 baseline + environment journal | `claude/baseline-run` | test journal only |
| B | Claude | §01 task lifecycle validation | `claude/task-lifecycle-validation` | issues/test notes; backend fixes only after operator approval |
| C | Claude | §02 translation-layer validation | `claude/translation-validation` | fixture findings; backend fixes only after operator approval |
| D | Codex | Rich Web UI implementation (§03) | `codex/rich-web-ui` | `src/pollypm/web_api/ui/*`, UI-focused Playwright specs |
| E | Codex | Performance harness (§06) | `codex/perf-harness` | `scripts/perf/*`, Makefile targets, perf docs |
| F | Codex | Smoke automation (§07) | `codex/smoke-automation` | `scripts/smoke*`, Makefile targets, lightweight CLI checks |
| G | Codex | Web UI test coverage | `codex/web-ui-playwright` | `tests/playwright/*` only |

Run A first. B, C, D, E, F, and G can start once A has the tested SHA and environment baseline.

---

## Ownership label flow

Labels apply to both issues and PRs:

| Current label | Who acts next | Typical action | Next label |
|---|---|---|---|
| `needs-codex` on issue | Codex | Implement or investigate | `needs-claude` on PR |
| `needs-claude` on issue | Claude | Implement or investigate | `needs-codex` on PR |
| `needs-codex` on PR | Codex | Review, request changes, approve, or merge | `needs-claude` only if changes needed from Claude |
| `needs-claude` on PR | Claude | Review, request changes, approve, or merge | `needs-codex` only if changes needed from Codex |

The ownership label always means "the next reviewer/actor," not "who authored the branch." Creator labels identify merge eligibility:

| Creator label | Who authored implementation | Who may review/merge |
|---|---|---|
| `codex-created` | Codex | Claude only |
| `claude-created` | Claude | Codex only |
| `mixed-agent-authors` | Both agents | Neither agent; split PR or operator merge |

Codex code-creation lanes therefore open PRs with `needs-claude` + `codex-created`. Claude validation/fix lanes open PRs with `needs-codex` + `claude-created`.

---

## Codex code-creation backlog

Codex should not just review §03; it should build the missing product affordances.

### D — Rich Web UI

Build:
- Task visibility panel using `/api/v1/tasks` and `/api/v1/tasks/{project}/{n}`.
- Task detail drawer with status, assignee, dwell time, labels, linked plan/review info, and recent context.
- State badges that match TUI semantics: working, waiting, idle, blocked, done, paused.
- Stuck-task surfacing with reason text, not just a red badge.
- Better empty/error/auth states, including token-expired messaging.
- Mobile layout polish: no horizontal scroll, stable keyboard behavior, reachable send/action controls.
- Client-side instrumentation hooks for click latency.

Architecture constraints:
- Keep UI code componentized by concern; do not turn `app.js` into a single growing god object if a small module/component boundary is available.
- Use public REST responses only; do not read files, PG rows, audit logs, or tmux state directly from UI code.
- If a backend contract is missing dwell time, stuck reason, or task linkage, file/label the contract gap instead of hardcoding a frontend inference that only works for one case.
- Keep instrumentation isolated so performance measurement can be disabled or ignored without changing UI behavior.

Tests:
- Playwright coverage for first load, surface selection, task panel render, detail drawer, daemon-down state, and auth error state.
- At least one M-scale fixture test or mocked large-response test so the UI does not only pass empty-state demos.

Do not change backend routes unless Claude’s §01/§02 validation confirms the API contract is wrong or insufficient.

### E — Performance Harness

Build:
- `scripts/perf/measure_http.py` or equivalent 30-sample runner.
- JSON/Markdown output with p50/p95/p99/max, status counts, and payload size.
- Named scenarios for dashboard, sessions, messages, task list/detail, claim, send, and inbox.
- Polling load runner matching §6.5.1.
- Resource snapshot command for `pm serve`, `pm cockpit`, PG connections, and file descriptors.

Architecture constraints:
- Keep perf harness code outside product runtime paths.
- Do not add test-only switches to production modules unless they are explicit, documented extension points.
- Report via files/stdout; do not mutate app state while measuring except in scenarios that explicitly test writes.

Tests:
- Unit tests for percentile calculation and non-2xx accounting.
- Dry-run mode that validates configuration without hitting the live daemon.

### F — Smoke Automation

Build:
- `make smoke` or equivalent script covering §07 REST checks, task create/queue/get, doctor, and sessions health.
- Clear red/green terminal output.
- A journal-friendly summary block with SHA, timestamp, and failed check.

Architecture constraints:
- Keep smoke automation as a thin orchestrator over public CLI/API commands.
- Do not import private product internals for checks that a real operator would perform via CLI/API.

Keep manual:
- Real browser console inspection until Playwright owns it.
- Real-phone ergonomics.

### G — Web UI Playwright

Build:
- Dedicated 1-second click-rule spec.
- Console-error capture.
- Network 4xx/5xx assertion.
- Mobile-chrome smoke.
- Trace capture on failure for perf issues.

Coordinate with D so tests target stable selectors and do not fight UI refactors.

Architecture constraints:
- Prefer user-visible selectors and stable `data-testid` hooks over brittle DOM traversal.
- Tests should assert public behavior and contracts, not private implementation details.

---

## Claude validation lanes

### A — Baseline

Output:
- Tested SHA.
- Automated suite status.
- Environment baseline from §00.6.
- Known failures vs novel failures.

Unlocks all other lanes.

### B — Task lifecycle

Output:
- Pass/fail matrix for §01.
- Contract gaps that block UI work.
- Self-heal-rule gaps.
- Any backend fix proposal requiring operator approval.

Claude should post API contract findings early, especially around task status names, assignee fields, dwell time, and stuck reasons.

### C — Translation layer

Output:
- Triple-witness fidelity findings.
- Transcript performance findings at small/medium/large history.
- Cache-pollution findings for `include_thinking` and `include_subagents`.
- Any endpoint behavior Codex UI must account for.

---

## Merge choreography

1. Land A findings first; all branches record the same baseline SHA.
2. Codex D/G open PRs with `needs-claude` + `codex-created`; Claude reviews, approves, and merges if green.
3. Codex E/F open PRs with `needs-claude` + `codex-created`; Claude reviews, approves, and merges if green.
4. Claude backend/test-plan fixes open PRs with `needs-codex` + `claude-created`; Codex reviews, approves, and merges if green.
5. Backend fixes from B/C land separately before UI depends on them.
6. After each merge, rerun §07 smoke.
7. Before ship/no-ship, run §06 M-scale once on merged main.

---

## Agent handoff template

Use this for every parallel worker:

```text
Owner:
Persona:
Branch:
Baseline SHA:
Creator label:
Ownership label:
Scope:
Files you own:
Files you must not touch:
Inputs from other lanes:
Deliverables:
Tests/verification:
Perf budget affected:
Architecture boundary touched:
Reviewer/merge agent:
Reviewer persona:
When to stop and ask:
```

---

## Stop conditions

Stop parallel work and regroup if:
- Two lanes need to change the same backend contract.
- §01 finds task lifecycle behavior that invalidates UI assumptions.
- §02 finds message ordering/cache behavior that invalidates Web transcript work.
- Any Codex UI path requires hiding a backend bug with frontend-only logic.
- M-scale §06 fails in a way that changes architecture, not just implementation.
- A lane needs to violate `architecture-guardrails.md` to make progress quickly.
