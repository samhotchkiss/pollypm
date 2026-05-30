# The 48-Hour Loop to a Magical PollyPM

**Status:** product-vision + operating loop, authored 2026-05-30, hardened 2026-05-30 after a readiness audit (4 lenses → `not-ready`) found the loop *measured* delight but had no engine to *generate* it, and that its exit gates depended on infrastructure that did not exist. Supersedes the reliability-only framing of `48h-reliability-loop.md` (now **Part III — the floor**). The point of running PollyPM for 48 hours is not to make it *not broken*. It is to make using it feel **magical** — and to keep it feeling that way every single time. Reliability is the floor magic stands on; it is not the goal.

> "It needs to be more than that. We want this experience to feel magical." — the operator, after being shown a reliability checklist.
> "We've got to figure out how to get to fucking Delight." — the operator, on what this loop is for.

---

## Part I — The vision: what a magical PollyPM *feels* like

Magic is a feeling, so describe the feeling, concretely, and then build toward it. Two people experience PollyPM: **the operator** (Sam, running the control plane) and **the end-user** (e.g. S.E. Elkins, whose project — savethenovel.org — PollyPM produces). Both should feel it.

**The operator's morning.** Sam opens the cockpit with his coffee. He does not see 1,597 inbox items. He sees a calm, beautiful one-screen brief, in plain language, like a note from a chief of staff who worked all night:

> *"Morning, Sam. Overnight: savethenovel shipped its last 3 chapters and the site went live — it's lovely, take a look. bikepath hit your Claude weekly limit at 2:14am; I rolled it to your backup account, no work lost, ~40% headroom left. Everything's flowing. **One thing needs you:** booktalk's plan wants a call on scope — here it is, with my recommendation. [Approve] [Tweak] [Tell me more]."*

He makes one decision in ten seconds and closes the laptop. The system **had it handled**, told him exactly what mattered, anticipated the rest, and made the one real decision effortless. He trusts it more than he did yesterday.

**The end-user's moment.** S.E. opens savethenovel.org and it stops her — warm paper textures, a hero that asks *"Which book changed your life?"*, type that feels like a beloved book. She didn't commission a website; she described a movement, and PollyPM produced something she's proud to put on a poster. The journey felt like collaborating with a thoughtful studio, not filing tickets.

**That's the bar.** Not "the dashboard renders." Not "the tests pass." *"It anticipated what I needed, handled what I didn't want to think about, surfaced the one thing that mattered beautifully, and the thing it made is genuinely good."*

---

## Part II — The experience principles (the texture of magic)

Every surface, message, and behavior is judged against these. They are how "magical" stops being a vibe and becomes design intent:

1. **Anticipation over reaction.** It surfaces the one thing that needs you *before* you go looking, and has already handled everything that didn't. The inbox is not a pile to triage; it is a curated "here's what matters" — usually empty, occasionally one clear thing.
2. **Self-narrating care.** It tells you, in plain warm language, what it did *for* you while you were away — especially the saves ("hit your limit, rolled over, no work lost"). You feel watched-over. Trust compounds.
3. **Calm beauty.** Surfaces are crafted, legible, quiet. Green = all handled, with grace. No noise, no walls of stale items, no scary stack traces. The cockpit feels as considered as the savethenovel site.
4. **Effortlessness.** The operator's job shrinks to: set direction, make the occasional *real* decision, enjoy the output. Babysitting is designed out. A goal in plain language becomes a finished thing — and the PM *decomposes and delegates it* rather than making you spell out the tasks.
5. **Invisible-but-legible recovery.** Things break; you'd never know — except it gracefully tells you it handled it. Failure becomes a trust-building moment, not an outage.
6. **Moments of delight.** It celebrates a shipped project. The copy has warmth and wit. It occasionally shows you something you didn't know you wanted (the book that changed the most lives this week). Small, human, surprising.
7. **Presence.** It feels alive and capable — a partner, not a tool. Latency is invisible (<1s, always); state feels live; it responds like it's *with* you.

If a surface or interaction doesn't serve at least one of these, it's friction or noise — file it as a `magic-gap:` item and design it out.

---

## Part III — The floor: reliability invariants (magic is impossible on a broken base)

You cannot feel watched-over by a system showing you 1,597 stale items, garbage decision cards, and inflated counts. So the reliability invariants are **non-negotiable prerequisites** — but passing them is not success, it's *eligibility* to be judged on magic.

Carry forward all invariants from `48h-reliability-loop.md` §2 (each requires a measurement artifact): **I1** surface cleanliness, **I2** honest counts, **I3** no silent accumulation, **I4** task flow, **I5** heartbeat self-heal *engages*, **I6** account failover, **I7** sessions/windows, **I8** cross-project isolation, **I9** perf/1s-click, **I10** agent acts-not-narrates, **I11** dogfood reaches done. Measured live, every cycle, no proxies. (That doc remains the detailed measurement reference; the invocation map in Part V says which numbered surface spec each invariant actually drives.)

---

## Part IV — The magic dimension (the ceiling — judged by feel, **backed by an artifact**)

Magic is judged the way the savethenovel site was judged: **use it and look — does it delight, or fall flat?** These are evaluated by *experiencing the product as the operator AND as the end-user*. **The reliability floor's iron rule applies here too: a cycle may not mark an M-test "pass" without producing the artifact.** Self-judged delight with no evidence is the same proxy-trap as green tests — and worse, because the judge (Claude) has a documented history of declaring the ugly site "done."

| # | Magic test | Evaluate by (use it, feel it) | **Required artifact** | The bar |
|---|---|---|---|---|
| **M1** | **One-glance clarity** | Open the cockpit cold as the operator. In 5 seconds: is everything handled? is there exactly one thing that needs me? | Screenshot + the operator's read-verdict | Calm, one clear ask or "all handled." Not a triage pile. |
| **M2** | **The morning brief** | Is there a plain-language "while you were away" narration of what it did *for* you (ships, saves, the one decision)? | Screenshot of the brief + verdict | A note you'd actually want from a chief of staff. Warm, specific, honest, short. |
| **M3** | **Self-narrating recovery** | Force a **real** failure via the chaos harness (failover, respawn). Does it tell you it handled it, gracefully + legibly? | The chaos-injection result **+** a before/after cockpit pane capture of the narration, tied to that real failure | "I caught X, did Y, you lost nothing" — trust-building, not silent, not scary. |
| **M4** | **Effortless intent** | Give Polly a real goal in plain language. Watch. | The goal text + the produced deliverable + the task-flow link (proving it decomposed+delegated, not did it in-session) | It plans, delegates, reviews, ships — you direct, you don't babysit. The output is genuinely good. |
| **M5** | **Beauty** | Look at every surface (cockpit, web UI, the deliverables it produces). Judge as a designer. | Screenshot of each surface judged | Crafted, calm, legible, gorgeous. "Would the operator/end-user be proud to show this?" |
| **M6** | **Delight moments** | Use it for one **session** (= one cycle's M-pass window). Did anything make you smile / feel anticipated / pleasantly surprised? | The quoted moment + timestamp | Passes a session iff **delights ≥ 1 AND ughs == 0.** |
| **M7** | **Trust over time** | Across the soak, does using it make you trust it MORE? | A per-cycle **1–5 trust reading** with one-line justification, appended to the journal/ledger | Passes the soak iff per-session trust readings **trend up with no single drop > 1.** A drop > 1 is a trust-breaking surprise = itself a `magic-gap:`. |

**No artifact = the M-test was not evaluated and cannot count toward the exit (Part VI.2).** "Session" = one cycle's magic-pass observation window.

---

## Part IV-A — The Delight Engine (the factory, not the inspector)

Part IV *measures* delight. This *manufactures* it. It is the answer to "how do we get to delight": a concrete, repeatable generative procedure that runs **once per cycle, after the reliability floor is green for that cycle**, and is **forced to terminate in one of two outcomes** — a shipped delight that moved a named M-test, **or** an evidenced "this surface is already at the bar" justification (a screenshot + why). *Running the magic pass, declaring "M1–M6 felt fine," building nothing, and journaling green is a **failed** magic pass.* Today the loop is an inspector; this makes it a factory.

**The five steps:**

- **Step 0 — GATE.** If any reliability invariant is RED this cycle, STOP the engine — it's a reliability-only cycle, delight waits. If the floor is GREEN, the engine MUST run and owes either a shipped delight or an evidenced no-gap. (This resolves the old contradiction between "pick ONE high-value action" and "build one delight every cycle.")
- **Step 1 — EXPERIENCE** (look as a human, on *current* code). First confirm live `/api/v1/health` `served_git_sha` == latest merge SHA — else `uv cache clean pollypm && uv tool install --reinstall-package pollypm --force`, restart serve+cockpit, re-confirm the SHA moved. *Never judge stale code.* Pick ONE surface this cycle, rotating across the soak: `{cockpit cold-open, morning brief, a recovery event, a task-flow moment, savethenovel desktop, savethenovel mobile}`. Drive it as the relevant human (operator for cockpit; first-time reader for the site). Screenshot. Write the honest one-line feeling: **delight / fine / ugh**.
- **Step 2 — LOCATE THE GAP** (merely-fine → magical). Name the *specific* distance from the bar, mapped to the M-test it offends and a Part II principle. E.g. *"cockpit opens to a correct but flat list — M1 / principle 3: counts honest but no one-glance 'all handled' framing"*; *"failover logged `account.failover.engaged` but the operator saw nothing warm — M3 / principle 2"*; *"savethenovel hero is centered text on white, accurate but lifeless — M5 / principle 6."* A "fine" surface with no nameable gap is allowed ONLY with a written reason it's already at the bar + the screenshot as proof.
- **Step 3 — DESIGN THE DELIGHT** (reach for a shipped magic skill *first*). Translate the gap into a concrete change AND name the skill(s) from the **71 shipped at `src/pollypm/plugins_builtin/magic/skills/`** that produce it:
  - operator briefs / copy / recovery narration → `internal-comms`
  - cockpit & web surfaces → `design-taste-frontend` + `frontend-design` + `brand-guidelines` + `extract-design-system`
  - savethenovel pages → `frontend-design` + `design-taste-frontend` + `visual-explainer` + `canvas-design` + `web-asset-generator`
  - render/screenshot for the look → `webapp-testing-playwright` / `browser-use-agent`

  If NO skill matches a *recurring* gap, author one via `skill-creator.md` (the "grow" half). **Output:** a one-paragraph buildable spec + the named skill(s) + the target M-test.
- **Step 4 — BUILD** (Codex, small focused PR, labeled). Dispatch a Codex worker with the spec, the named skill(s) to apply, the as-is screenshot, the target M-test, and the `magic-gap` + `needs-codex` labels. Claude reviews + merges (no self-merge). RC-stage discipline: small PRs.
- **Step 5 — LAND ON A HUMAN** (the *only* step that earns credit). After merge, re-confirm live `served_git_sha` == merged SHA, re-experience the **same** surface, re-screenshot, compare before/after against the targeted M-test, write the verdict + a 1–5 trust reading. **Only now** write the delight-ledger row. A PR that merged but moved no M-test (or made it worse) does **not** discharge the cycle's obligation — reopen the gap.

**Quality bar (anti-cosmetic-churn).** A delight counts only if it (a) targets a named M-test, (b) is built from a magic skill or a justified new one, (c) is verified live (`served_git_sha` == merge SHA) to *move* that M-test, and (d) survives the next cycle's M-pass with no new ugh. Cosmetic churn that moves no M-test does not discharge the cycle.

**Throughput floor (anti-wind-down for the magic pass).** ≥ 1 M-test-moving delight per ~4h of green-floor time, or exit criterion 2 cannot be claimed. **Delight-liveness guard:** if the floor has been green ≥ 2 cycles with no delight shipped, that is a RED signal — journal it AND PushNotify (not a silent pulse).

**The two durable artifacts:**
1. **The delight-ledger** — `docs/test-plan/journals/delight-ledger.md`, one row per gap: `{cycle, surface, M-test, principle, as-is gap + screenshot, skill(s) used, Codex PR, live-SHA@verify, after-screenshot, M-test moved?, trust 1–5}`. This **is** the evidence for exit criterion 2 and the M7 trajectory. **No row = no delight credit.** Seeded at cycle-0 with one row per baseline gap.
2. **GitHub labels** — `magic-gap` (open delight debt), `delight-shipped` (merged AND verified to move an M-test), `m1`..`m7` (which M-test a gap serves). Created before the run.

**The highest-leverage growth item — the run's FIRST `magic-gap:`.** The 71 magic skills each declare `when_to_trigger` frontmatter, but `magic/plugin.py` loads only a static deploy prompt — **runtime auto-surfacing is unwired**, so no worker building savethenovel is offered `design-taste-frontend`/`visual-explainer` unless an agent names it by hand. Wiring `when_to_trigger` matching makes *every* agent inherit taste skills for free — the single biggest delight multiplier across the fleet. File it first (see issue tracker), build it, and the engine's Step 3 gets dramatically stronger.

---

## Part V — The loop, reframed

Same bounded-cycle mechanics as `48h-reliability-loop.md` §3 (load+look first, measure, verify live, journal with evidence, never wind down) — but each green-floor cycle now runs **two passes**: the **reliability pass** (the floor — are the invariants holding?) and the **delight engine** (Part IV-A). A cycle that only fixed bugs and built no delight (with no evidenced no-gap) is a **failed magic pass**; a cycle that declared magic without *using* the product is no cycle.

**Per-cycle contract (deterministic):**
- **Floor RED** → reliability-only cycle; the engine is paused; the only valid action is driving the red invariant to live-verified green.
- **Floor GREEN** → the engine MUST run and owes a delight or an evidenced no-gap.
- **Hard time-box.** A tick must end inside its interval — dispatch long work (chaos runs, big builds) to a subagent and end the tick; don't block the driver.
- **Wall-clock.** Each tick computes and journals **"hour N of 48"** from a recorded run-start. **No terminal verdict before hour 48** unless Part VI is fully met with artifacts.
- **Liveness floor.** ≥ 1 evidence-bearing cycle per hour. A content-free pulse ("standing by", a bare counter) does **not** count and is itself flagged (the documented `pulse-close` antipattern).
- **Deploy precondition (printed at the top of BOTH passes, every cycle).** Live `served_git_sha` == merged SHA, else `uv cache clean pollypm && uv tool install --reinstall-package pollypm --force` → restart → re-confirm. Any M-test or perf number measured on an unverified SHA is **void**.

**Invocation map (which numbered surface spec each invariant actually drives — pinned by filename, since the magic loop's bare "§2/§3" refer to the *sibling* reliability doc, not these):**

| Invariant | Driven by | Cadence |
|---|---|---|
| **Cycle-0 baseline** | `00-pre-flight-baseline.md` — freeze SHA + account inventory + all I3 metrics + a Delight Baseline (screenshot+score every surface as-is) | once, before K starts |
| I1 surface cleanliness | `03-web-ui-richness.md` + `01-task-lifecycle.md` §1.4 | every cycle |
| I4 task flow | `01-task-lifecycle.md` §1.1–1.5 | every cycle |
| I5/I6/I7 self-heal | `05-resilience-recovery.md` §5.2/§5.5.2/§5.6 + `tests/chaos/` | chaos rotation |
| I9 perf | `06-performance-budgets.md` + `perf-harness.md` (measure **at rest** — record concurrent load with each number) | on deploy + spot checks |
| I10 agent quality | `04-agent-behavior.md` + `agent-personas.md` | sampled each cycle |
| I11 dogfood | `01-task-lifecycle.md` §1.5 + `02-translation-layer.md` + the dogfood look (Part VI.3) | continuous |

**Roles:** Claude operates + experiences + judges (taste, like the site critique) + files reliability bugs and `magic-gap:` items + reviews/merges Codex PRs (worktree-isolated reviewers) + verifies every fix live. Codex builds. The taste calls (is this magical?) are Claude's to make honestly — but Part VI.3 forbids Claude rationalizing on the end-user's behalf, and Sam's periodic taste-check is the ground truth that overrides.

**Chaos rotation & soak:** as `48h-reliability-loop.md` §3/§6 — but note the chaos harness must **exist and be validated at cycle-0** (it is a precondition, not a mid-run TODO). The 48h IS the soak; every cycle records the accumulation metrics against the cycle-0 baseline.

---

## Part VI — Exit: "magical, every goddamn time"

The 48h loop is complete only when, **with evidence**, ALL hold:

1. **The floor holds** — all reliability invariants (Part III) pass by measurement, for **K ≥ 6 *trailing contiguous* cycles** (see the K-reset rule in `48h-reliability-loop.md` §7 — ANY red I1/I2/I4/I8, ANY chaos self-heal miss, or live ≠ merged SHA resets K to 0) AND under the full chaos rotation (failover/session/task-flow injected ≥ 3× each, self-heal every time), with no silent accumulation across the soak (standing piles trending **down**, not merely flat).
2. **The ceiling is reached** — the magic tests (Part IV) pass *by artifact*: paired **baseline→final screenshots** per surface prove M1/M2/M5; M3 has a real chaos-injection + narration capture; M4 has a goal→deliverable→task-flow link; M6/M7 are evidenced from the **delight-ledger**, which shows ≥ 1 M-test-moving delight per ~4h of green-floor time and **zero open `magic-gap:` items that lack either a skill or a fix.** "Reached delight" cannot be claimed while an unresolved `magic-gap:` blocks an exit-gate M-test.
3. **A real end-user would be delighted** — the dogfood (savethenovel) is judged not "live" but *delightful*, by a **concrete procedure** (no more "build+served = done"):
   1. confirm the live savethenovel SHA == merged;
   2. screenshot **every page at desktop AND mobile** widths via `webapp-testing-playwright`/`browser-use-agent`;
   3. score against a written **beauty rubric** (typography, hierarchy, whitespace, hero emotional hook, imagery-not-placeholder, copy warmth) using `design-taste-frontend` as the evaluator lens;
   4. **anti-rationalization rule:** the verdict MUST cite specific visual evidence (the screenshots) AND name ≥ 1 concrete flaw — *a flawless, no-evidence "she'd love it" verdict is auto-rejected* — or file `magic-gap:dogfood-*` and dispatch a fix. Closing this gate on build/deploy success is forbidden.
4. **A signed recommendation** in the journal citing the evidence for the floor (per-criterion artifacts) **and** the delight-ledger for the ceiling, dated at or after hour 48.

If it's reliable but not magical, **it is not done.** That is the entire point of this rewrite.

---

*Reliability was the answer to "how did you think this was finished?" The Delight Engine is the answer to "how do we get to fucking delight?" — it turns "is it magical?" into "here is how we manufacture one more piece of magic, with evidence, every green-floor cycle." The loop must serve that, standing on the floor, and it must be felt — by a real operator and a real user — every time.*
