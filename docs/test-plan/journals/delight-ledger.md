# Delight Ledger — the durable store for the Delight Engine

**Purpose.** This file is the evidence for **Part IV-A** (the Delight Engine) and **Part VI.2** (the magic exit gate) of `../48h-magic-loop.md`, and the home of the **M7 trust trajectory**. It exists because "delight rose over the soak" was unfalsifiable from memory — structurally the same hole the reliability floor closed with audit artifacts. **No row here = no delight credit.** The M7 verdict and exit criterion 2 cite this ledger, not recollection.

**How it's used.** Every **green-floor** cycle, the engine ends by either appending a row (a shipped+verified delight) or recording an evidenced no-gap line. Cycle-0 seeds one **baseline row per identified gap** (status `open`, no after-state yet). A delight is only `shipped` once Step 5 (LAND ON A HUMAN) confirms — live `served_git_sha` == merge SHA, re-experienced, the targeted M-test moved.

**Companion GitHub labels** (create before the run): `magic-gap` (open delight debt), `delight-shipped` (merged AND verified to move an M-test), `m1`..`m7` (which M-test a gap serves).

---

## Row schema

| Field | Meaning |
|---|---|
| `cycle` | cycle index + "hour N of 48" |
| `surface` | cockpit cold-open / morning brief / recovery event / task-flow / savethenovel desktop / savethenovel mobile |
| `M-test` | which of M1–M7 this gap offends |
| `principle` | which Part II principle (1–7) |
| `as-is gap` | the specific distance from magical + link/path to the **as-is screenshot** |
| `skill(s)` | which `magic/skills/*` were reached for (or new skill authored via `skill-creator`) |
| `Codex PR` | the PR that built it |
| `live-SHA@verify` | the `served_git_sha` at the LAND step (must == PR merge SHA) |
| `after` | link/path to the **after screenshot** |
| `M-test moved?` | yes/no — did the targeted M-test actually improve? (no ⇒ reopen, no credit) |
| `trust 1–5` | the per-cycle trust reading after this surface |
| `status` | `open` / `building` / `shipped` / `reopened` / `no-gap (evidenced)` |

---

## Trust trajectory (M7)

One line per cycle: `cycle N (hour H): trust=X/5 — <one-line justification>`. M7 passes the soak iff readings trend **up** with **no single drop > 1** (a drop > 1 is a trust-breaking surprise = a new `magic-gap:`).

_(append below)_

---

## Cycle-0 — Delight Baseline

_Seed at cycle-0: drive + screenshot + honestly score every operator surface and savethenovel as-is; record M1–M7 verdicts with named "ughs"; set starting `trust=N`; create one `open` row per gap found. Until this is done, the engine has no t=0 anchor and "merely-fine → magical" claims diff against nothing._

| cycle | surface | M-test | principle | as-is gap | skill(s) | Codex PR | live-SHA@verify | after | moved? | trust | status |
|---|---|---|---|---|---|---|---|---|---|---|---|
| _0_ | _(seed at baseline)_ | | | | | | | | | | open |

---

## Ledger rows

_(append shipped/verified delights below, newest last)_

| cycle | surface | M-test | principle | as-is gap | skill(s) | Codex PR | live-SHA@verify | after | moved? | trust | status |
|---|---|---|---|---|---|---|---|---|---|---|---|
