# PollyPM Ship-Readiness Test Plan

**Purpose:** verify PollyPM is ready to ship — not just that it works, but that it works **reliably**, **fast**, **intuitively**, and **feels magical** to users.

This is not a unit test suite. Unit tests live under `tests/` and are run by `pytest`. This plan is for the kind of verification you do *before declaring ready-to-ship*: a structured pass through the system that catches the things automated tests miss — kludge, latency, confusion, "huh that's weird" moments, magic-feeling that's actually broken.

**User contract:** the user/operator is never expected to run CLI commands to make the product usable. CLI, `curl`, SQL, and filesystem probes in this plan are for testing agents only: setup, instrumentation, failure injection, and diagnosis. A user-facing scenario does not pass just because a command works; it passes when the behavior is visible and usable through Web UI, TUI, inbox, notification, or another product surface.

## How to use

Each section lives in its own file and is **self-contained**. You should be able to open `01-task-lifecycle.md` cold, in a fresh session with no prior context, and execute it end-to-end. Sections reference each other by filename, never by "as discussed."

At least some testing must be done exactly as a user would interact with the product:
- **Web UI:** drive flows through Playwright and, for exploratory checks, a real browser.
- **TUI:** send keystrokes to `pm cockpit` in tmux (or use Textual `pilot`), not direct service calls.
- **CLI/API probes:** allowed only as tester instrumentation; they cannot be the sole evidence for an intuitive or magical pass.

For multi-agent execution, use **`parallel-execution.md`**. It defines which lanes Claude owns, which lanes Codex owns, and where Codex should produce actual product code rather than only review.

For code architecture, use **`architecture-guardrails.md`**. PollyPM is modular and plugin-based; ship-readiness fixes must preserve those boundaries, not work around them.

For agent identity and role expectations, use **`agent-personas.md`**. Testing agents, coding subagents, and reviewing agents each have different responsibilities and merge permissions.

For the repo-watching Codex agent, use **`codex-watcher-instructions.md`** as the exact operating prompt.

For the Claude autonomous testing loop, use **`claude-loop-instructions.md`** — invoked via `/loop` with each tick executing one bounded unit of testing/review/fix work. That doc is the operating procedure; this README and §§00–07 are what the loop EXERCISES.

For the operator workflow this plan must validate, read **`operator-day-in-the-life.md`** first. It anchors the abstract test scenarios in a concrete day of real usage. If a scenario in this plan does not trace to something in that doc, ask whether it's the right scenario.

For session results, use the journal template at **`journal-template.md`**. Every test session writes a journal entry to `docs/test-plan/journals/<YYYY-MM-DD>-<short-label>.md`. Without a journal, the session didn't happen.

**Order:**
1. **`00-pre-flight-baseline.md` must pass first.** If pytest, Playwright, or `pm doctor` is red on main, every downstream observation in this plan is unreliable. Fix the baseline before you start.
2. Run §§01–05 to prove the product is correct, coherent, and resilient.
3. Run **`06-performance-budgets.md` at M-scale before any ship/no-ship call.** Performance is not optional evidence; it is a release gate.
4. **`07-quick-smoke.md`** is the 15-minute daily-driver — run it any time before merging to verify main is shippable.

**Pace:** the full plan is a 24+ hour engagement. Don't rush. The point is to find the rough edges, not to speed-run a checklist. If a section reveals something interesting, follow the thread — even if it takes you outside the scripted steps.

## Time-budget guides

The full plan is the goal. When you have less, use one of these slices.

### 2 hours — "is anything obviously broken?"

1. **§07 quick smoke** (15 min) — confirms main is shippable today.
2. **§01.5 cascade scenarios** (45 min) — the highest-leverage correctness check.
3. **§03.5 1-second click rule** sweep (30 min) — covers the headline UX invariant.
4. **§06.3 user-facing budgets**, Web cells only (30 min) — measure click latency under realistic data.

Output: a short journal entry that says "shippable / not-shippable / blocked on X."

### 8 hours — "do the critical paths really work?"

Add to the 2-hour slice:
- Full **§01** (task lifecycle, all subsections)
- Full **§02** (translation layer, triple-witness)
- **§05.1 + §05.2 + §05.4** (daemon kill, pane kill, pause-marker enforcement).
  §05.2 only counts when run with a managed PollyPM control plane or explicit
  scheduler/test dispatcher; an isolated `pm serve` run is unverified evidence.
- **§04.1 + §04.3** (canonical role prompts + auth-marker)
- **§06.3 + §06.4 + §06.7** at S-scale

Output: a journal entry that classifies each axis (functional, reliable, fast, intuitive, magical) per critical path.

### 24 hours — the full plan

Run everything. Include:
- M-scale seeding (§06 setup) before the perf section.
- Real phone for §03.10 and §06.3 phone cells.
- §06.8 soak (4+ hours background while you do other sections).
- §04.6 long-context drift (only feasible if there's a long architect session running).

Output: ship/no-ship recommendation in the journal, all `bug:` / `flake:` / `perf:` / `ux:` / `magic-gap:` issues filed with reviewer-label set, promotion-status updated, follow-up sprint backlog drafted.

## The headline invariant: 1-Second Click Rule

**Anywhere a user clicks in the TUI or Web UI, the response must complete within 1 second.**

This is non-negotiable. Any click that breaks 1 second is a ship-blocker — file `perf:click-latency:<action>` immediately. See `06-performance-budgets.md` for measurement methodology, scale targets, and p95/p99 reporting.

## Performance release bar

PollyPM is only "highly performant" when it passes `06-performance-budgets.md` at **M-scale**:

- 20 active surfaces.
- 500 tasks.
- 5,000 total transcript messages, including one >1MB surface.
- 3 desktop tabs plus 1 real phone.
- p95 within budget, no unexplained 5xx, no resource leak during soak.

S-scale is smoke coverage. L-scale is headroom. **M-scale is the promised-land gate.**

## How to run as the testing agent

The agent running this plan (the one doing the thinking, executing scenarios, deciding what's broken) uses **Opus**. Don't downgrade.

Sub-agents you spawn for mechanical work use **Sonnet** when the work is contained and rule-following:
- Rebases against `main`.
- Doc/comment updates.
- Stale-comment cleanups.
- Running pytest and reporting output.
- Re-applying a known fix pattern.

Sub-agents stay on **Opus** when the work requires judgment:
- Touching task lifecycle invariants (§01).
- Modifying the heartbeat cascade (§01.5, §05).
- Changing storage boundaries (§02).
- Anywhere "what's the right scope" is uncertain.

When in doubt, default to Opus. Wrong fixes on critical paths cost more than tokens.

See `fix-flow.md` for the full PR + sub-agent dispatch protocol.

## The five axes

Every scenario in every section evaluates against five criteria:

| Axis | Question | If failed |
|---|---|---|
| **Functional** | Does it produce the right output? | File a `bug` issue |
| **Reliable** | Does it work the *same way* every time? | File a `flake` or `race` issue |
| **Fast** | Does it complete within the perf budget? (§06) | File a `perf` issue |
| **Intuitive** | Can an operator do it without reading docs? | File a `ux` issue |
| **Magical** | Does it feel like the system anticipated the need? | File a `magic-gap` issue |

A scenario can pass functionally and still fail on reliability or magic. **All five axes count.**

## Fix flow

When something breaks, follow **`fix-flow.md`**. Short version: `needs-codex` and `needs-claude` identify the next responsible agent; `codex-created` and `claude-created` identify who authored the PR. The tagged agent acts, then hands off to the other agent. The agent that writes a PR cannot approve or merge it.

## Automation promotion

When a manual scenario passes once, decide whether it graduates to pytest or Playwright. See **`automation-promotion.md`**. The bar: anything that catches a regression we'd actually ship without it.

## Glossary (in case you've never read PollyPM code)

- **Operator** — the human running the system; that's you.
- **Polly** — the meta-agent that watches over everything; sees what's broken and dispatches recovery.
- **PM** — per-project manager agent; reasons about that project's tasks and state.
- **Architect / Advisor / Worker** — agent roles, one per session, each with a different system prompt.
- **Task** — a unit of work in the Work Service; core lifecycle is draft → queued → in_progress → review → done, with rework / blocked / on_hold / cancelled side paths.
- **Surface** — a chat-like view onto a session's transcript; what the Web UI shows.
- **Heartbeat** — per-session "I'm alive" ping; missed heartbeats trigger the recovery cascade.
- **Heartbeat cascade** — health-check tiers: heartbeat (mechanical) → PM (project reasoning) → Polly (operator). The testable contract is documented in §01 and §05.
- **TUI** — the Textual terminal interface (`pm cockpit`).
- **Web UI** — the v0 browser interface served by `pm serve` at `/ui/`.
- **Inbox** — operator-addressed messages: plan reviews, alerts, fake-injection cleanup, etc.

## Sections at a glance

| File | What it verifies | Time | Promotion target |
|---|---|---|---|
| `00-pre-flight-baseline.md` | pytest, Playwright, `pm doctor` are green on main | 30 min | already automated |
| `01-task-lifecycle.md` | task state machine, concurrency, visibility, heartbeat recovery | 4–6 h | pytest integration + manual UX |
| `02-translation-layer.md` | TUI ↔ storage ↔ REST fidelity, no drift | 2–3 h | pytest |
| `03-web-ui-richness.md` | Web UI matches TUI and adds value | 4–6 h | Playwright + manual UX |
| `04-agent-behavior.md` | agents respond usefully, not just renderably | 2–4 h | evals harness |
| `05-resilience-recovery.md` | system survives failure injection | 2–4 h | integration tests + chaos |
| `06-performance-budgets.md` | latency/CPU/memory inside firm thresholds at realistic scale | 3–4 h + soak | perf harness + CI gates |
| `07-quick-smoke.md` | 15-min daily-driver shippable check | 15 min | partially automated |

Total active execution: ~16–28 hours. Real wall-clock: 24+ hours when you include bug-fix loops, cross-device setup, phone testing, and following interesting threads.

## What success looks like

You finish this plan and you can answer **"yes"** to:

1. Every task you assigned ended up in the state you intended, without manual nudging.
2. Every message you sent showed up where you expected, in the right order, within the latency budget.
3. **No click in TUI or Web UI took longer than 1 second.** (Per the headline invariant.)
4. The Web UI showed you everything the TUI did, with at least one affordance the TUI doesn't have.
5. When you broke things on purpose, the system recovered itself (or asked clearly when it needed you).
6. A fresh operator could sit down at the Web UI and accomplish their first task without reading documentation.
7. The M-scale performance gate passed with recorded p50/p95/p99/max and no resource leak.
8. The agents responded usefully to canonical prompts (not just rendered messages).
9. You used the system long enough that you forgot you were "testing" — and it kept working.

If any of those is "no," the gap is documented as an issue and on the next sprint's backlog.

## Ship / no-ship decision

This plan exists to support a binary decision. Use these criteria to convert journal entries into a recommendation.

**Ship — green:**
- §00 baseline green (or known-pre-existing only).
- §01: all of 1.1, 1.2, 1.3 pass; §1.5 cascade self-heals without manual intervention; §1.6 lifecycle perf within M-scale budget.
- §02 triple-witness shows zero drift on the sampled scenarios.
- §03 1-second click rule met for every interaction tested.
- §04 canonical prompts pass per role; auth-marker contract enforced; no confident hallucination.
- §05 daemon kill, pane kill, pause-marker fail-closed all recover within budget.
  Pane-kill evidence must include the §05.2 control-plane prerequisite; `pm serve`
  alone cannot make this gate green.
- §06 M-scale gate green with recorded p50/p95/p99/max for every cell in the budget table.
- §07 smoke green on the merge commit being shipped.

**Yellow — ship with caveats, requires explicit operator approval:**
- All Ship criteria EXCEPT one of:
  - One `magic-gap:` issue surfaces that affects new operators but not existing ones, filed with owner + deadline.
  - One `perf:` issue affects a non-critical click cell, observed degradation < 50% over budget, filed with owner + deadline.
  - §06 L-scale headroom is short but M-scale passes.

**Red — do not ship:**
- Any §01 cascade self-heal requires manual operator action.
- Any §02 triple-witness scenario shows drift between pane, archive, and REST.
- Any §03 click breaks the 1-second rule.
- §05.2 is reported from an isolated `pm serve` run without a managed control
  plane or explicit scheduler/test dispatcher.
- Any §05 fail-closed scenario fails open (e.g., malformed pause marker silently allows recovery).
- §5.5.2 Claude subscription failover does not happen automatically, or loses session context.
- Any §06 M-scale endpoint p95 above budget, or any payload above budget.
- Any §04 auth-marker false-positive (unmarked impostor actioned) or false-negative (legitimate marker refused).
- Any unresolved `bug:` issue on the §07 path.

The decision goes in the journal, signed by the testing persona (per `agent-personas.md`). If the recommendation is yellow or red, the journal entry must include the explicit set of issues blocking ship and proposed remediation owners.

## Stop conditions during the run

Stop and ask the operator if:
- A scenario reveals a behavior that invalidates the assumptions of a later section (e.g., task lifecycle finds that "claim" doesn't atomically transition — every later UI test depends on that).
- A fix would require changing task lifecycle invariants, the heartbeat cascade, storage boundaries, or plugin architecture.
- A perf result is so bad that more investigation is wasted effort until the architecture is reconsidered.
- The system is producing real data (e.g., recovery actually sending messages to live agents) that you suspect is not a test environment.

Do not silently absorb surprising findings. Surface them.
