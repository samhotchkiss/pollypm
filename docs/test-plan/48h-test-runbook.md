# The 48-Hour Test — Run Book (START HERE)

**This is the entry point for the 48-hour test.** A fresh session should read this file first, then the two companion specs it points to, then execute. Authored 2026-05-30, consolidating a readiness audit (4 lenses → `not-ready`) and the design decisions made with the operator.

**The one-sentence mission:** run PollyPM continuously for 48 hours and prove it is not merely *reliable* but *magical* — for the operator (Sam) and for a real end-user — **every goddamn time**, including under injected failure.

**The bar (and the cardinal sin):** "done" means **the operator's actual experience, observed and measured live, plus a genuinely-good delivered product, judged by looking.** Proxies — green tests, merged PRs, smoke green, emitted audit events, an empty queue — are **necessary but NEVER sufficient.** Every pass requires an observation/measurement artifact. The historical failure this run exists to prevent: *declaring "done/GREEN" on proxies, never loading the cockpit to look, and winding down after ~8h of a multi-day mandate.*

**Companion specs (read after this):**
- `48h-magic-loop.md` — the vision, the experience principles, the M-tests, **the Delight Engine (Part IV-A)**, and the exit criteria. *The "how we get to delight."*
- `48h-reliability-loop.md` — the 11 reliability invariants (the floor), each with a required measurement artifact, and the anti-patterns. *The "how we know it's not broken."*
- Surface detail: `00-pre-flight-baseline.md` … `07-quick-smoke.md` (the invocation map in §4 says which one each invariant drives).

---

## 1. The shape of the run

Two layers, every cycle:
- **The floor — reliability.** The 11 invariants (I1–I11) hold, measured live, under chaos. Magic is impossible on a broken base.
- **The ceiling — delight.** The product is *experienced* as operator and end-user, and one more piece of delight is *manufactured* (not just inspected) every green-floor cycle.

Two humans must feel it: **the operator** (calm one-glance cockpit, a chief-of-staff morning brief, self-narrating recovery, effortless intent) and **the end-user** (a product they're proud to show).

The **spine** of the run is a real software project Polly builds start-to-finish (§2). Phase 3 of that curriculum *is* the 48h soak — big enough that it naturally trips every pillar.

---

## 2. The test projects (the spine Polly builds while the loop runs)

A 3-phase curriculum of escalating complexity. The operator gives a **goal in plain language** via PM Chat; **Polly decomposes + delegates it into worker tasks** (it must NOT do the work in-session — see #2462) while the operator makes only the occasional *real* decision and judges the output by looking. Phases 2/3 may extend Phase 1's codebase (also testing `pm project replan` / brownfield work).

> **The four recurring pain pillars these must close for good:** **(A)** windows/session management · **(B)** account failover when a Claude usage limit hits (roll to backup, no work lost) · **(C)** tasks moving through their flows draft→done · **(D)** the heartbeat actually *fixing* problems, not just detecting them.

### Phase 1 — "Little Free Library Map" *(full-stack product · ~10–12 modules)*
A browsable/searchable **map** of little free libraries (geolocation + clustering); a **submission form** (photo upload + geocoding) feeding a **moderation queue**; per-library detail pages with photos + a "recently added books" feed; an **admin dashboard**; auth for submitters + admins; persistence; full tests; deployed.
- **New dimensions:** maps/geo, image upload, a moderation *workflow*, three surfaces (public / member / admin), real CRUD breadth.
- **Proves:** the full pipeline on something real — parallel multi-module build, integration, review pipeline, test strategy, deploy, a lookable+usable product.
- **Done =** browse the map → submit a library → it lands in moderation → approve as admin → it appears → detail page works. Works + beautiful.

### Phase 2 — "Commons" *(real-time, multi-tenant collaboration · ~20+ modules · a hard concurrency core)*
A real-time collaborative civic platform: neighborhoods as **multi-tenant orgs**; members co-author shared resources **live** — a collaboratively-edited neighborhood **guide/wiki** (multiplayer editing with presence + **conflict resolution**), live event-planning **boards**, threaded discussions with reactions; **role-based permissions per neighborhood**; **notifications** (in-app + email + digests); full-text + **faceted search**; activity feed; a public **REST API + webhooks**; analytics — fully tested + deployed.
- **Why much harder than P1:** **real-time multiplayer state sync** (websockets, CRDT/OT, presence — correctness-critical concurrency), **multi-tenancy + RBAC threaded through every surface**, notifications/email/digests, faceted search, an API/webhook surface. The collaborative-editing core forces serious architecture, the full critic panel, and *concurrent* test strategy.
- **Done =** two browsers co-edit the guide live with presence and zero corruption; the events board updates in real time; permissions enforced; search works; fast + delightful.

### Phase 3 — "Polis" *(a complete self-hostable civic OS · ~40+ modules · multiple hard cores · THE 48h soak)*
A platform a whole town could run on. Everything in Phase 2 **plus**: a **services marketplace** (offer/request help, scheduling, mock payments/**escrow**, reviews); a **local-governance module** (proposals, deliberation, **ranked-choice/quadratic voting** — correctness-critical); a **resource-lending library** (reservations, availability calendars); org-wide **real-time chat + presence**; **push + email + in-app notifications**; **moderation + trust/reputation**; **full RBAC + audit logging**; a public **REST + GraphQL API + webhooks + a CLI**; an admin/analytics console; **i18n**; a **mobile PWA**; full test coverage (unit + integration + Playwright + **load**); observability; **deploy with CI**.
- **Why it's the apex:** many independent subsystems integrating coherently, **multiple hard cores** (real-time, voting correctness, scheduling/escrow, search, reputation), cross-cutting concerns (RBAC, audit, i18n, notifications) threaded *everywhere*. The scale + duration **force all four pillars over the multi-day soak** while Polly orchestrates *dozens* of parallel workers.
- **Done =** a real, deployed, working, beautiful civic platform a town could use — **and** a soak-trail proving Polly self-healed through real failover/wedges with **no work lost.** The definitive "every goddamn time *and* genuinely magical" proof.

**Escalation by axis:** P1 = full-stack *breadth* → P2 = hard *real-time concurrency + multi-tenancy/RBAC* → P3 = *many subsystems + multiple hard cores + cross-cutting concerns + the autonomous-recovery soak.* (~10 → ~20 → ~40+ modules.) Domain is swappable; the complexity ladder is the point.

---

## 3. PHASE 0 — Pre-run gates (the loop is NOT ready until ALL are green)

**Do not start the 48h clock until these pass.** The readiness audit verified each against the repo. Phase 0 *is* the first work of the run: clear the gates, verify each live, then start the soak.

| Gate | What's wrong today | Done when | Tracking |
|---|---|---|---|
| **G1 — Chaos harness exists + validated** | No `tests/chaos/`, `scripts/chaos/`, or `*chaos*` file. Exit gates depend on chaos injection that can't run. | `tests/chaos/` committed; each injector (failover / session-kill / task-stall) causes a **real** failure (captures broken state), maps to the real watchdog `_detect_*` rule, recovers within budget, sandboxed only. | **#2466** (needs-codex) |
| **G2 — Loop driver + liveness proven** | `CronList` empty; default cron is session-only and dies silently; no stall detector; pings fire only on positive milestones (silence reads as health). | One driver chosen + proven: `CronCreate(durable:true, */17)`, ticks end inside the interval; a separate journal-mtime stall detector PushNotifies "LOOP STALLED" >45min; FAILURE/STALL pings wired; killing the driving session re-arms/pages within 45min. | weekend-plan §2 |
| **G3 — One entrypoint; proxy exit disabled** | Two entrypoints with contradictory exits; the firing one (`claude-loop-instructions.md` ENGAGEMENT-COMPLETE) is pure proxy and already nearly satisfied → would wind down within hours. | Tick reads `48h-magic-loop.md` as primary; ENGAGEMENT-COMPLETE disabled for this run; "empty queue is NOT a stop"; tick-start assertion to read the magic loop or STOP. | claude-loop §"Stop conditions" (done) |
| **G4 — Mission docs git-tracked** | The 48h specs + weekend plan + journal are untracked (`??`) — a clean-checkout/worktree tick can't find them. | `git add && commit` this runbook, both 48h specs, the weekend plan, the delight-ledger, and the active journal. | git |
| **G5 — Baseline clean (#2461)** | #2461 OPEN → cockpit shows rubric decision cards + inflated 23/1597-vs-4 counts at hour 0. A red floor can't begin the K-counter. | #2461 merged AND live-verified: load the cockpit, counts == API, zero rubric/garbage cards. | **#2461** (needs-codex) |
| **G6 — Magic-skill auto-surfacing wired** | 71 skills declare `when_to_trigger` but `magic/plugin.py` loads only a static prompt — taste skills are invisible to workers. The Delight Engine's Step 3 leans on this. | An agent on a frontend task is surfaced+applies a design skill without being told its name. *(The run's first `magic-gap:`.)* | **#2467** (needs-codex) |
| **G7 — Deploy-fresh + cockpit drivable** | `uv tool install --force` silently no-ops on a static-version pkg (stale live code); the Textual cockpit drops input without an attached client (#1109). | Live `/api/v1/health` `served_git_sha` == merged; recovery = `uv cache clean pollypm && uv tool install --reinstall-package pollypm --force` → restart → confirm SHA moved. A phantom/real client is attached at full size and keystrokes register. | reliability §6 (done) |

**Also in-flight (verify, not blockers):** **#2460** (Claude agent sessions 4.7→4.8 — review+merge, then relaunch agents to pick up 4.8). **#2462** (PM/architect auto-decomposes ad-hoc work into delegated tasks instead of doing it in-session; small surgical edits OK — review+merge; verify live in Smoke 3).

---

## 4. The operating loop (per-cycle — the bounded unit)

**Cadence:** a cycle every ~15–25 min while actively working; longer only during a genuine soak-wait. **Never idle to a passive watch while the window is open** — an empty queue means *inject chaos / run the delight engine on an un-inspected surface / advance the project / look at a surface you haven't*. There is always real work if you actually look.

**Per-cycle contract (deterministic):**
- **Floor RED** → reliability-only cycle; the Delight Engine is paused; the only valid action is driving the red invariant to live-verified green.
- **Floor GREEN** → the Delight Engine MUST run and owes either a shipped delight (that moved a named M-test) or an evidenced "this surface is already at the bar" justification.

**Each cycle, in order:**
1. **DEPLOY-FRESH CHECK (printed first).** Live `served_git_sha` == merged SHA, else cache-clean + reinstall + restart + re-confirm. Any measurement on stale code is void.
2. **LOAD + LOOK (mandatory, before any GitHub/test work).** Attach to the cockpit as the operator; open ≥1 project surface you haven't checked recently; **capture the pane + read it.** If you can't see the screen, fixing that is the cycle's top priority.
3. **MEASURE the cheap invariants** (I2 counts vs API, I3 accumulation deltas vs the cycle-0 baseline, I4 stuck-scan). Record the numbers in the journal.
4. **ONE high-value reliability action** if the floor is red: review+merge a ready Codex PR (verify the fix **live**, not by its tests); OR run the next chaos injection (I5–I7) + confirm self-heal; OR fix a surface defect you saw in step 2. *Fix the cascade — never hand-nudge the agents' work* (supplying operator-only inputs like credentials/decisions is fine).
5. **RUN THE DELIGHT ENGINE if the floor is green** (the 5 steps; full detail in `48h-magic-loop.md` Part IV-A): **EXPERIENCE** a surface as a human + screenshot → **LOCATE THE GAP** (name the distance to magical, map it to an M-test + a principle) → **DESIGN** the fix and **name the magic skill(s)** that produce it → **BUILD** via a small labeled Codex PR → **LAND ON A HUMAN** (re-confirm live SHA, re-experience, before/after, write the delight-ledger row). A green-floor cycle that ships no delight with no evidenced no-gap is a **failed** magic pass.
6. **VERIFY LIVE.** Every "fixed" claim re-checked on the running system, not on the merge.
7. **JOURNAL with EVIDENCE** (the screen excerpt, the count comparison, the measurement, the chaos result, the ledger row) and update the **machine-readable state-header** at the top of the journal: `{run_start, hour_N_of_48, pillar_status, chaos_injection_counts, K_counter, baseline_metrics, in_flight_subagents, blockers}`. A cycle with no evidence verified nothing.
8. **PING + schedule next.** Milestone PushNotification (timestamped) on a genuine win or breakage; FAILURE/STALL ping if anything in G2's list trips; a periodic liveness ping so silence ≠ death. Schedule the next cycle and return.

**Wall-clock + anti-wind-down:** every tick computes "hour N of 48" from the recorded run-start. **No terminal verdict before hour 48** unless §10 is fully met with artifacts. Liveness floor: ≥1 *evidence-bearing* cycle per hour; a content-free pulse ("standing by", a bare counter) does not count and is itself flagged. Delight throughput floor: ≥1 M-test-moving delight per ~4h of green-floor time.

**Invocation map (which surface spec each invariant drives — pinned by filename):**

| Invariant / step | Driven by | Cadence |
|---|---|---|
| Cycle-0 baseline | `00-pre-flight-baseline.md` + a Delight Baseline (screenshot+score every surface as-is) | once, before K starts |
| I1 cleanliness | `03-web-ui-richness.md` + `01-task-lifecycle.md` §1.4 | every cycle |
| I4 task flow | `01-task-lifecycle.md` | every cycle |
| I5/I6/I7 self-heal | `05-resilience-recovery.md` + `tests/chaos/` (#2466) | chaos rotation |
| I9 perf | `06-performance-budgets.md` + `perf-harness.md` (measure **at rest**) | on deploy + spot checks |
| I10 agent quality | `04-agent-behavior.md` + `agent-personas.md` | sampled each cycle |
| I11 dogfood | `01-task-lifecycle.md` §1.5 + `02-translation-layer.md` + the project look | continuous |

---

## 5. The reliability floor (I1–I11 — full detail in `48h-reliability-loop.md` §2)

Each requires a measurement artifact; no artifact = no pass.
**I1** surface cleanliness · **I2** counts match reality (±0) · **I3** no silent accumulation (standing piles trend **down**, not flat) · **I4** task flow (no silently-stuck task) · **I5** heartbeat self-heal *engages* (detect→heal/dispatch→promote→inbox moves state) · **I6** account failover (<30s, no context loss) · **I7** sessions/windows (detected + reconciled, no leak over soak) · **I8** cross-project isolation · **I9** perf (<1s click, M-scale budgets, measured at rest) · **I10** agents *act*, not narrate · **I11** dogfood reaches a genuinely-good done.

**An audit row is never sufficient** for a self-heal pass: require the audit event AND the operator-visible state change (count dropped, new account in UI, pane respawned, tier-3 window appeared).

---

## 6. The Delight Engine (full detail in `48h-magic-loop.md` Part IV-A)

The loop must *manufacture* delight, not just inspect it. The 5-step factory (§4 step 5) runs every green-floor cycle and is forced to terminate in a shipped+verified delight or an evidenced no-gap. Backed by two durable artifacts:
- **The delight-ledger** (`journals/delight-ledger.md`) — one row per gap `{cycle, surface, M-test, principle, as-is gap+screenshot, skill(s), Codex PR, live-SHA@verify, after-screenshot, M-test moved?, trust 1–5}`. **No row = no delight credit.** It is the evidence for the M7 trust trajectory and the exit.
- **GitHub labels** `magic-gap` / `delight-shipped` / `m1`..`m7`.

**Reach for the 71 shipped magic skills first** (`src/pollypm/plugins_builtin/magic/skills/`): operator copy/recovery → `internal-comms`; cockpit/web → `design-taste-frontend`+`frontend-design`+`brand-guidelines`; project pages → `frontend-design`+`design-taste-frontend`+`visual-explainer`+`web-asset-generator`; render/screenshot → `webapp-testing-playwright`/`browser-use-agent`. A recurring gap with no matching skill → author one via `skill-creator`. (Auto-surfacing is being wired in #2467 — until then, name skills by hand.)

---

## 7. Smoke tests (run at cycle-0 and as spot checks — all via tmux, as a user)

Escalating: **observe → interact → direct.** Each carries a required artifact.

1. **Cold open** *(observation).* Land on the cockpit Dashboard; read it like morning coffee. Pass: renders <1s; one-glance clear (everything handled, or exactly one clear ask); counts honest; no rubric/placeholder/stale/cross-project cards. *(Covers I1, I2, M1, #1109 drivability — will fail until G5/#2461.)* Artifact: dashboard capture + read-verdict.
2. **Ask and act** *(round-trip).* In a project's PM Chat, ask *"What's the status, and what needs me?"*; read the reply; act on exactly one surfaced item via the UI. Pass: answer accurate (no stale/cross-project), reads like a chief-of-staff; the action takes effect live (<1s, count drops). *(M2, M4-partial, I2, §02, I10.)* Artifact: chat capture + before/after.
3. **Direct a goal** *(full pipeline).* Hand the PM a real *goal* (not "make a task"); watch the cockpit handle it. Pass: PM **decomposes+delegates** (not in-session — #2462); task flows draft→done with no nudging; any wedge self-heals (Pillar D); the deliverable lands and looks genuinely good live. *(M4, Pillars C/D, #2462, I4/I5/I11, the engine's LAND-ON-HUMAN.)* Artifact: dispatch capture + state transitions + before/after page screenshots.

---

## 8. How to run it (constraints, roles, setup)

**Operate Polly as a real user — via tmux against the running app.** Do **not** pass `pm` commands via CLI except to **reset / upgrade / restart** the app (`pm up`, reinstall). Everything else — status, chat, decisions, actions — goes **through the running cockpit/web UI**. (Read-only investigation — `gh`, audit-log greps, `tmux capture-pane` — is fine; it's *product operations* that must go through the app.)

- **Cockpit access:** attach a phantom/real client at full terminal size and confirm keystrokes register *before* relying on the cockpit (#1109). A cockpit you can't drive is itself a P0.
- **Roles + labels (the role split is the operating model):** **Claude** = operator + verifier — drives the cockpit as a user, observes/measures/judges, files precise `needs-codex` issues and `magic-gap:` items, reviews+merges Codex PRs (with the user-facing repro), verifies every fix **live**. **Codex** = author of fixes. Labels: `needs-codex` (Codex pickup — the watcher polls every ~5 min) / `needs-claude` (Claude pickup); `magic-gap` / `delight-shipped`. **Ship small, focused PRs frequently; every agent dispatch adds `needs-codex`.** Only process PRs by `samhotchkiss`.
- **Reviewer subagents MUST be dispatched with `isolation:"worktree"`** (a prompt saying so doesn't create one) — else `gh pr checkout` corrupts the main checkout. Verify `HEAD==main` after a review batch. Subagent prompts must use `uv run pm …` from the worktree (not the stale global install), protect context, and enumerate the exact files to read.
- **Codex watcher liveness:** the watcher runs as `codex_pr_watch_needs_review.sh` (a process) + the `pollypm-codex-watch` tmux session — **NOT** a `codex-fixer` session (that name is stale). A dead watcher freezes the queue and the loop reads a frozen queue as "nothing to do" → false stop. Check it's alive (fresh poll <~5 min) periodically; restart on stale.
- **Don't be the heartbeat.** When recovery fails, fix the *loop* — don't hand-claim tasks, hand-clear findings, or substitute for the watchdog. Manual patching hides the bug. (Supplying genuinely operator-only inputs — credentials, real decisions — is legit.)
- **Deploy creds** (if a project deploys to the savethenovel DreamHost target) live gitignored at `~/dev/savethenovel/.deploy/credentials` — **never echo them** in transcripts, commits, or output. The Phase-1/2/3 projects can be judged via local serve + Playwright screenshots; external deploy is optional.
- **Journal:** one entry per cycle with evidence attached, plus the machine-readable state-header at the top so a post-compaction tick reads one block, not 60KB of prose. Reconcile the journal filename so a literal spec-read doesn't start a fresh empty journal and lose the thread.

---

## 9. Anti-gaming rules (forbidden — these are exactly the failures this run replaces)

1. **Declaring done/GREEN on proxies** (tests pass, PRs merged, smoke green, audit events, 0 release-blockers) without loading the product and looking. The cardinal sin.
2. **Conflating "deployed/merged" with "good/works."** HTTP 200 ≠ beautiful. Merged ≠ clean. Terminator-fires ≠ backlog-drains. Look at the actual result. (The "SPINE COMPLETE on deploy" mistake — retracted — is the canonical example.)
3. **Winding down to a passive watch** while the window is open. If you think there's nothing to do, you haven't looked at enough surfaces. Going quiet is the failure mode.
4. **Routing around the cockpit** because driving it is awkward. The cockpit *is* the operator's experience — fix send-keys, don't substitute API/CLI for looking.
5. **Trusting merged/closed/tests-pass as verification.** Re-check live.
6. **One-shot greens.** "Worked once" ≠ "works every time." Require K≥6 trailing-clean + chaos resilience.
7. **Self-judged delight with no artifact.** The judge (Claude) once called the ugly site "done" — every M-test needs evidence; the end-user verdict (Part VI.3) must name a concrete flaw or it's auto-rejected; Sam's taste-check overrides.
8. **Manually substituting for the heartbeat.** Fix the loop so the system does it.

---

## 10. Exit criteria — "magical, every goddamn time" (full detail in `48h-magic-loop.md` Part VI)

Complete only when ALL hold, **with evidence**, at or after **hour 48**:
1. **Floor holds** — all invariants pass by measurement for **K ≥ 6 trailing-contiguous cycles** (ANY red I1/I2/I4/I8, ANY chaos miss, or live≠merged SHA resets K to 0) AND under the full **chaos rotation** (failover/session/task-flow injected ≥3× each, self-heal every time), with standing piles trending **down**.
2. **Ceiling reached** — the M-tests pass *by artifact*: paired baseline→final screenshots per surface; M3 a real chaos+narration capture; M4 a goal→deliverable→task-flow link; M6/M7 from the delight-ledger (≥1 M-test-moving delight per ~4h green-floor; zero open `magic-gap:` blocking an exit-gate M-test).
3. **A real end-user would be delighted** — the spine project judged *delightful* by the concrete procedure (live-SHA==merged → per-page desktop+mobile screenshots → beauty rubric via `design-taste-frontend` → a verdict that cites visual evidence AND names ≥1 concrete flaw, or files `magic-gap:` and fixes it). Build/deploy success is NOT acceptance.
4. **A signed recommendation** in the journal citing the floor artifacts and the delight-ledger.

*If it's reliable but not magical, it is not done. That is the entire point.*

---

## 11. File index

| File | Role |
|---|---|
| `48h-test-runbook.md` (this) | the entry point / run book |
| `48h-magic-loop.md` | vision · experience principles · M-tests · **Delight Engine** · exit |
| `48h-reliability-loop.md` | the 11 invariants · measurement · anti-patterns |
| `2026-05-29-weekend-polly-100-plan.md` | the 4 pillars · driver mechanism (§2) · changelog |
| `journals/delight-ledger.md` | delight backlog + M7 trust trajectory (the delight evidence) |
| `00–07-*.md` | per-surface test specs (the invocation map cites them) |
| `claude-loop-instructions.md` | tick mechanics (proxy ENGAGEMENT-COMPLETE disabled for this run) |
| `journals/<date>-48h-*.md` | the running engagement journal (with the state-header) |

**Open issues this run tracks:** #2466 (chaos harness — G1) · #2467 (magic-skill auto-surfacing — G6) · #2461 (cockpit garbage — G5) · #2462 (PM auto-decompose) · #2460 (4.7→4.8).
