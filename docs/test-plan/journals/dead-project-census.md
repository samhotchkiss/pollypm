# Dead-Project Census — decision-ready (48h soak, hour ~9.5, 2026-05-30 12:36 MDT)

**Purpose.** Consolidates the scattered dead-project findings (cycle 48 draft pile, cycle 50 itsalive dormancy) into one table Sam can act on, and gives Codex the liveness dataset for **#2480** (operator-count inflation). Live PG (`work_tasks`), read-only.

## The core problem
The operator "N things need you" count + cockpit rail are inflated by **dead/synthetic projects** carrying stale nonterminal task piles (998 drafts total, ~85% from dead projects; plus stale blocked/queued). The urgent display symptom is already fixed (inbox honest at 4, #2470), but the underlying pile + rail clutter remain.

## ⚠️ Key insight for #2480 — `last_activity` is a FALSE liveness signal
`max(updated_at)` makes dead projects look LIVE (idle 0), because the **watchdog keeps touching them** — minting `draft`/`queued` hygiene tasks about already-dead work. So a project can show "active today" while having done zero real work for weeks.
A robust liveness signal for the count semantics = **"has a non-watchdog real-work event (a `done` task, or a human/agent-driven transition) within N days"**, NOT raw last-touch. Use `last_done` + tracked-flag, not `max(updated_at)`.

## Census (nonterminal-bearing projects)

| project | nonterm | drafts | blocked | queued | last real-work (`done`) | verdict |
|---|---|---|---|---|---|---|
| **myproj** | 636 | 515 | 0 | 112 | never | 🔴 SYNTH — archive |
| **queuestorm_1779777233** | 201 | 101 | 0 | 100 | never | 🔴 load-test debris — archive* |
| **inbox** | 124 | 124 | 0 | 0 | 05-06 (none since) | 🔴 stale — archive |
| polly_remote | 123 | 112 | 9 | 2 | 05-03 | 🟡 abandoned-real (KEEP, but its 112 drafts are stale) |
| savethenovel | 37 | 36 | 0 | 1 | 05-29 | 🟢 LIVE (dogfood) |
| **test_5_8_x** | 30 | 29 | 0 | 1 | never | 🔴 SYNTH — archive |
| pollypm | 29 | 0 | 0 | 29 | 05-25 | 🟢 LIVE |
| **demo** | 26 | 25 | 0 | 1 | never | 🟡 demo — Sam-decide |
| smoketest | 25 | 2 | 0 | 23 | 05-26 | 🟡 smoke harness — Sam-decide |
| **proj** | 19 | 18 | 0 | 1 | never | 🔴 SYNTH — archive |
| samblog | 17 | 1 | 7 | 9 | 05-18 | 🟢 real (blocked are 05-18 stale) |
| itsalive | 17 | 2 | 8 | 7 | 05-23 | 🟡 DORMANT-real — blocked = 8 stale "Module N Contract" @ 05-18 |
| **fresh** | 10 | 10 | 0 | 0 | never (38d idle) | 🔴 SYNTH — archive |
| russell | 8 | 4 | 0 | 4 | 05-14 | 🟢 real |
| **testpause_1779773565** | 6 | 6 | 0 | 0 | never | 🔴 SYNTH — archive |
| health_coach | 6 | 1 | 0 | 5 | 05-18 | 🟢 real |
| media / booktalk / coffeeboardnm | 3/3/2 | — | — | — | varies | 🟢 real (small) |
| pr2316_drift_* / pm_test_*wave* | 2–3 ea | — | — | — | 05-30 (harness) | 🟡 test fixtures — Sam-decide (may be active) |
| **second** | 2 | 2 | 0 | 0 | never (8d) | 🔴 SYNTH — archive |
| race_proj_2317 / ghost / beta / alpha / real | 1–2 ea | — | — | — | idle 3–5d | 🔴 SYNTH — archive |
| **hexwar / polly_e2e_proj / pollypm_cycle_ux_scratch / pomodoro / demo_polly** | 1 ea | — | — | — | never (11–38d) | 🔴 ancient — archive |

\* `queuestorm_*` and `pr2316_drift_*`/`pm_test_*wave*` look like **active test-harness fixtures** (timestamped names, touched 05-30) — confirm they're not load-bearing for the chaos/perf seeds before archiving.

## Recommended actions
1. **High-confidence archive** (synthetic, never completed real work, idle ≥4d): `myproj`, `inbox`, `test_5_8_x`, `proj`, `fresh`, `testpause_*`, `second`, `race_proj_*`, `ghost`, `beta`, `alpha`, `real`, `hexwar`, `polly_e2e_proj`, `pollypm_cycle_ux_scratch`, `pomodoro`, `demo_polly`. → drains ~700–860 drafts + de-clutters the rail.
2. **Sam-decide:** `queuestorm_*`, `pr2316_drift_*`, `pm_test_*wave*`, `smoketest`, `demo` — likely active test fixtures; archive only if the harness no longer needs them.
3. **Keep + clean piles:** `polly_remote` (112 stale drafts), `itsalive`/`samblog` (stale 05-18 blocked) — these are real projects whose OLD task piles should be reclaimed by the watchdog drain (#2472) once it works, not archived.
4. **#2480 fix:** liveness-based count/rail semantics using `last_done`+tracked, NOT `max(updated_at)` (which the watchdog poisons).

_Not an autonomous mutation — archiving is Sam's call (some are deliberate fixtures). This is the decision artifact._
