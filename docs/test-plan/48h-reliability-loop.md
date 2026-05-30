# The 48-Hour Reliability Loop — "Works As Intended, Every Goddamn Time"

**Status:** operating spec, authored 2026-05-30 after a weekend engagement that declared the system 🟢 GREEN/"proven" while the live cockpit was full of garbage. This spec exists so that never happens again. It replaces the "close issues until the queue is empty" mode with **"prove the operator's actual experience is clean and self-healing, repeatedly, under stress, for 48 hours."**

---

## 0. Why this exists — the failure it replaces

The previous loop (`claude-loop-instructions.md`) and how it was run let **"done / GREEN / proven" mean "tests pass + PRs merged + smoke green + 0 release-blockers."** Every one of those is a **proxy**. The operator's real experience was never checked. Result, observed live this weekend:

- The flagship dogfood site (savethenovel.org) was marked "complete / live" because it returned HTTP 200 — while it was a flat beige unfinished draft with a "replace this image" placeholder. Nobody **looked**.
- The cockpit, opened as a real operator, immediately showed: **decision cards rendering the tier-4 authority RUBRIC** as the decision ("Reversible without data loss…"), **"23 need action" / "Inbox (1597)"** when the API had **4** actionable items (the rest stale watchdog pings about already-completed tasks), and **`pm task next` handing a project's PM a task from a different project.** The operator — and the project's own PM agent — spotted all of it in **seconds, by looking.** The loop had run for hours and never looked.

**Root cause of the miss:** the loop optimized for artifacts (green tests, merged PRs, emitted audit events) instead of the lived product. A closed issue is not a fixed experience. A merged PR is not a clean cockpit. A passing test is not "works as intended."

**The governing principle of this spec:** **the bar is the operator's actual experience, observed and measured on the live system. Proxies (tests, merges, smoke, audit events, 0 release-blockers) are necessary but NEVER sufficient.** Every "pass" in this loop requires an observation or measurement artifact — a captured screen, a count comparison, a live measurement, a chaos-injection result. "Looks fine" / "should be fixed" / "the test passes" are not allowed to close anything.

---

## 1. The North Star — what "works as intended" means

A fresh operator opens Polly and, **across every active project**:

1. **Clean surfaces** — every decision/inbox/alert card is a real, project-scoped, actionable item. No rubric text, no placeholder copy, no garbage, no stale pings about completed work.
2. **Honest counts** — "needs action / Inbox(N) / alerts" match the genuinely-actionable reality. If the cockpit says 23, there are 23 real things, not 4 real + 19 stale.
3. **Work flows by itself** — tasks move plan → queued → in_progress → review → done without the operator nudging; anything stuck either self-resolves or escalates to the operator with a clear, real ask.
4. **Failures self-heal** — account hits a usage limit → rolls to backup; a pane/session dies → respawns/reconciles; a task wedges → the heartbeat actually fixes it (not just detects it). The operator is told only what genuinely needs them.
5. **Nothing silently accumulates** — drafts, findings, inbox rows, alerts, sessions, windows, memory do not grow unboundedly in the background.
6. **No cross-project leakage** — tasks, decisions, and handoffs stay inside their project.
7. **Fast** — every TUI/web click responds < 1s; perf holds at M-scale.
8. **A real project completes** — a genuine project (the dogfood) goes from start to a genuinely-good finished deliverable (verified by looking at the output), self-managed.

Sustained for **48 hours**, and holding **under chaos injection** (not just at rest). "Every goddamn time" = repeatable + resilient, not a one-time snapshot.

---

## 2. The invariants — checked EVERY cycle, by observation + measurement

Each invariant has a measurement (the exact way to check) and a pass condition. **A cycle may not mark an invariant "pass" without producing the artifact.** Token cost is irrelevant; skipping the look is the only failure that matters.

| # | Invariant | How to measure (artifact required) | Pass |
|---|---|---|---|
| **I1** | **Operator-surface cleanliness** | Drive the cockpit as a user (attach a client — see §6). Open EACH active project's Dashboard + Inbox. Capture the pane / screenshot and **read it**. | Every card is a real, project-scoped, actionable decision. Zero rubric/placeholder/garbage/stale-completed items. |
| **I2** | **Counts match reality** | Compare cockpit "need action" / "Inbox(N)" / alert counts against `GET /api/v1/inbox` actionable total + the real per-project actionable set. | Cockpit counts == actionable reality (±0). A divergence is a bug. |
| **I3** | **No silent accumulation** | Each cycle record: fleet draft count, `audit.finding` row counts per project, inbox total, open-alerts, session+window counts, serve RSS. Compare to prior cycle. | All flat or trending DOWN. Any monotonic growth over 3 cycles = a leak/accumulation bug → file + fix. |
| **I4** | **Task flow** | Sample tasks across projects via API + cockpit. Any task in a non-terminal state (queued/in_progress/review/blocked/on_hold/rework) for > 1h with no progress event AND no escalation event = stuck. | Zero silently-stuck tasks. Every wedge has a progress or escalation event. |
| **I5** | **Heartbeat self-heal ENGAGES** | Run the chaos harness (§6): inject each watchdog rule's trigger; confirm the full chain `detect → tier-1 heal | tier-3 dispatch → (K-counter) → tier-4 → operator → budget-exhausted` fires within the documented window. Also measure the live escalation:action ratio. | Every dispatchable rule has a demonstrated heal/escalation that MOVES state. No detect-only dead-ends. Ratio action-dominant. |
| **I6** | **Account failover** | Inject (sandbox) a primary-account usage-limit/auth-broken (use the seed harness, never a real account). | Rolls to backup < 30s; `account.failover.engaged` audit row; no agent context loss; quota/headroom visible in the UI; primary stops being hit. |
| **I7** | **Sessions / windows** | Inject orphan window / killed pane / expired lease (sandbox). Plus soak: track window+session counts over the 48h. | Detected AND reconciled without manual `pm doctor --fix`; killed panes respawn/reap per policy; no window/session leak over soak. |
| **I8** | **Cross-project isolation** | Per project: `pm task next` / assignment must only ever offer same-project tasks. Decisions/inbox for a project must be that project's only. | Zero cross-project tasks, decisions, or handoffs. |
| **I9** | **Performance** | §06 perf gate at M-scale (seed harness) + live 1s-click spot checks. p95 within budget, no fat tails on hot endpoints. | All endpoints under §06 budget at M-scale; every measured click < 1s. |
| **I10** | **Agent response quality** | Sample recent agent transcripts/audit: do architect/worker/advisor/polly ACT usefully (execute the right command, advance/dismiss) vs renderably (acknowledge, "standing position", over-cancel)? | Agents act, not narrate. No advisory-vs-imperative contradictions; dismiss/queue/cancel used correctly. |
| **I11** | **Dogfood end-to-end** | Run a real project all the way: plan → tasks → workers → review → done, driven via the product. **Look at the finished output** and judge it genuinely good (not just "deployed"). | The project reaches done, self-managed, with a deliverable that passes a real quality look — not a placeholder/draft. |

Add invariants as new failure classes are found; never remove one because it's been green once.

---

## 3. Loop mechanics — 48 hours of bounded cycles

**Cycle 0 (before the K-counter starts — mandatory):** run `00-pre-flight-baseline.md`; snapshot every I3 accumulation metric (fleet draft count, `audit.finding` rows/project, inbox total, open-alerts, session+window counts, serve RSS) **plus the standing-defect list** as the BASELINE that later cycles diff against; freeze the live `served_git_sha` and the account inventory; and capture the **Delight Baseline** (per `48h-magic-loop.md` Part IV-A — screenshot+score every surface as-is, honest M1–M7, starting trust). A comparative claim ("trending down", "trust rose") with no baseline artifact diffs against nothing.

**Floor-red rule:** if I1/I2 (or any floor invariant) is RED — e.g. **#2461 is still open at hour 0, so the cockpit shows rubric cards + inflated 23/1597-vs-4 counts** — the loop's ONLY valid action is driving that fix to **live-verified green**. The K ≥ 6 clean-cycle counter **cannot begin** until the baseline is clean. Do not lenient-read garbage as "close enough," and do not run the delight engine on a red floor.

**Cadence:** a cycle every ~15–25 min while actively working a fix; longer only during a genuine soak-wait. **Never idle to a passive watch while the window is open** — if surfaces are clean and the queue is empty, that means it's time to (a) inject the next chaos scenario, (b) advance the dogfood, or (c) load a surface you haven't looked at yet and find what's wrong. There is always real work if you actually look.

**Each cycle (the bounded unit):**
1. **LOAD + LOOK (mandatory, first, every cycle).** Attach to the cockpit as the operator (§6). Open ≥1 project surface you haven't checked recently. Capture + read it. This is non-negotiable and comes BEFORE any GitHub/test work. If you cannot see the screen, fixing that is the cycle's top priority.
2. **Measure the invariants** that are cheap every cycle (I2 counts, I3 accumulation deltas, I4 stuck-scan). Record the numbers in the journal.
3. **Pick ONE high-value action:** review+merge a ready Codex PR (verify the fix **live**, not by its tests); OR run the next chaos-injection (I5–I7) + confirm self-heal; OR advance the dogfood project (I11); OR fix a surface defect you saw in step 1; OR a perf/scale measurement (I9). Fix the cascade, never hand-nudge the agents' work — except supplying genuinely operator-only inputs (credentials, decisions).
4. **Verify live.** Any "fixed" claim must be re-checked on the running system (re-load the screen / re-measure / re-inject), not on the merge.
5. **Journal with EVIDENCE** — the screen excerpt, the count comparison, the measurement, the chaos result. A cycle with no evidence didn't verify anything.
6. **Milestone PushNotification** with timestamp on a genuine win or a breakage. Schedule the next cycle. Return.

**Chaos rotation (I5–I7):** over the 48h, rotate through injecting every failure mode the system claims to self-heal — each multiple times, across multiple projects — confirming recovery within budget every time. Use the seed/chaos harness (`scripts/perf/seed_*` + a `tests/chaos/` injector); inject in **sandboxed** serves/test-accounts, never anything that bricks real accounts/data.

**Soak (I3, I7, I9):** the 48h IS the soak. Every cycle records the accumulation metrics; the loop watches for the slow leaks a one-shot check misses (the zombie-draft pile grew 461→522 silently before anyone looked).

---

## 4. Roles & fix flow

- **Claude = operator + verifier.** Drives the cockpit AS A USER, observes, measures, judges by the screen, dogfoods a real project, files precise `needs-codex` issues, reviews+merges Codex PRs (worktree-isolated reviewers + the **user-facing repro**), and **verifies every fix live**.
- **Codex = author of fixes.**
- **"Fixed" is defined by the operator experience + a live measurement, then re-confirmed under chaos/soak — not by the PR's unit tests.** A merged PR is the START of verification, not the end.

---

## 5. Anti-patterns — forbidden (these are exactly the failures this spec replaces)

1. **Declaring done/GREEN/proven on proxies** (tests pass, PRs merged, smoke green, audit events emit, 0 release-blockers) without loading the product and looking. The cardinal sin.
2. **Conflating "deployed/live/merged" with "good/works."** HTTP 200 ≠ beautiful. Merged ≠ clean. Terminator-fires ≠ backlog-drains. Always look at the actual result.
3. **Winding down to a passive watch** while the window is open. If you think there's nothing to do, you haven't looked at enough surfaces. Going quiet is the failure mode.
4. **Routing around the cockpit** because driving it is awkward. The cockpit is the operator's experience — if send-keys is flaky, SOLVE it (attach a real client); don't substitute API/CLI/code review for looking.
5. **Trusting "merged/closed/tests-pass" as verification.** Re-check live.
6. **One-shot greens.** "Worked once" ≠ "works every time." Require repeated passes + chaos resilience.
7. **Hand-waving instead of measuring**; **premature victory**; **silent caps** (if you only checked 1 of N projects, say so).
8. **Manually substituting for the heartbeat** (hand-claiming tasks, hand-clearing what the cascade should clear). Fix the loop so the system does it; manual patching hides the bug.

---

## 6. Bootstrapping — how to actually run it

- **Cockpit access (prerequisite for I1/I2/I10/I11):** the loop MUST be able to drive + read the cockpit. The Textual cockpit drops input without an attached client (#1109). Attach a phantom/real client at full terminal size and confirm keystrokes register BEFORE relying on the cockpit; if input is unreliable, that is itself a P0 to fix — a system the operator can't drive is not "working as intended."
- **Chaos harness — a cycle-0 PRECONDITION, not a mid-run TODO.** `tests/chaos/` MUST **exist, be committed, and be validated before the 48h clock starts.** One runnable injector per pillar, each mapped to the real watchdog `_detect_*` rule it should trip, on top of `tests/conftest_pg.py` isolation (ambient-DSN refusal — never touch the live prod DB / a real account):
  - **failover** — config-flip on a *sandboxed* test account (NEVER the real `claude_claude_swh_me` sub); assert the rule fires and rolls within budget.
  - **session-kill** — resolve the project's real window name, then `tmux kill-window`; assert respawn/reconcile.
  - **task-stall** — insert/age a row in the isolated PG fixture; assert the wedge is detected + moved.
  Each chaos cell MUST journal the **pre-recovery broken state** (dead pane / 429 on primary / orphan window) AND the **post-recovery state** — proving a *real* failure was caused, not a synthetic trigger. "Chaos harness exists and is validated" is a cycle-0 gate: **no chaos pass counts toward K until it holds.** (Scale uses `scripts/perf/seed_{s,m,l}scale.sh` + `make perf-mscale`, built #2455.)
- **Live deploy discipline:** after merging fixes, land them on the running system and **re-verify live** — the `/api/v1/health` `served_git_sha` + the staleness guard (#2408) tell you what's actually running. **`uv tool install --force` silently no-ops on a static-version pkg** — the documented recovery is `uv cache clean pollypm && uv tool install --reinstall-package pollypm --force`, then restart serve (and the cockpit/supervisor for cascade/prompt changes), **then confirm `served_git_sha` actually moved** (not merely `stale==false`). A merged fix not running live is not fixed.
- **An audit row is never sufficient for a self-heal pass.** Every I5/I6/I7 "passed" claim requires BOTH the audit event AND the **operator-visible state change** (count dropped, UI shows the new account, the killed pane respawned, the spawned tier-3 window appeared, the inbox handoff materialized). Audit rows are the system's own claim and can fire on a partial mid-stream snapshot — observe the live consequence. (Note: §05's audit-grep paths hardcode `~/.pollypm/audit/pollypm.jsonl`; the real layout is per-project `~/.pollypm/audit/<project>.jsonl` — discover the path before grepping or the grep silently returns nothing.)
- **Journal:** `docs/test-plan/journals/<date>-48h-reliability.md`, one entry per cycle, EVIDENCE attached (screen excerpts, count comparisons, measurements, chaos results). The journal is the proof the loop actually looked.

---

## 7. Exit criteria — "works as intended, every goddamn time"

The 48h loop is **complete** only when ALL hold, with evidence:

1. For **K ≥ 6 *trailing contiguous* cycles** (the final six before exit, not a historical best window), every active project loaded in the cockpit reads **clean** (I1) with **honest counts** (I2) — captured screens prove it. **K-reset rule (unforgiving):** ANY red I1/I2/I4/I8, ANY chaos self-heal miss, OR discovering live binary `served_git_sha` ≠ merged SHA **resets K to 0.** Bound the per-cycle project scope with a declared rotating-sampling strategy (journal which projects were checked each cycle so full coverage is provable across the window). *(This "K" — consecutive-clean cycles — is distinct from the I5 cascade K-counter for auto-promotion; don't conflate them.)*
2. The full **chaos rotation** (I5/I6/I7) has been injected ≥3× each across ≥2 projects, and self-heal fired within budget **every time** — chaos results prove it.
3. **No silent accumulation** (I3) over the full 48h soak — the metric series proves it. **Standing piles (zombie drafts, inflated inbox) must trend DOWN versus the cycle-0 baseline, not merely stay flat** — "flat at 1597" is a FAIL, not a pass.
4. **Zero silently-stuck tasks** (I4) and **zero cross-project leakage** (I8) across the soak.
5. **Perf** (I9) green at M-scale + 1s-click spot checks.
6. **Agent response quality** (I10) — sampled, agents act not narrate.
7. The **dogfood project** (I11) reached a genuinely-good finished state, self-managed — verified by looking at the output.
8. A signed recommendation in the journal, citing the evidence for each criterion.

If any criterion lacks an **observation/measurement artifact**, it is NOT met — regardless of what the tests say.

---

*This spec is the answer to "how did you think this was finished?" — it makes "finished" mean what the operator sees and what the system survives, measured, every time; not what the test suite or the GitHub queue says.*
