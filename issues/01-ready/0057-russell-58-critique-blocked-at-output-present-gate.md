# russell/58 critique blocked at output_present gate

**Deliverable shipped:** Maintainability critique committed to task/russell-58 at `docs/plan/critique_maintainability.json` (commit `d4707c6`).

**Headline finding:** `candidate_a/b/c.md` are stale relative to russell/53. They were authored for the russell/19 vertical pivot Sam has now rejected. Scored A=3, B=6, C=2, implicit_russell_53_target=6. Verdict: architect should publish `candidate_d.md` for the actual Roster-frame re-plan. 11 risks + 11 recommended changes — top P0s: rewrite `scripts/check-pitch-content.mjs` in lockstep with HTML (~37/51 assertions fingerprint discarded vocabulary), align `scripts/m5-verify.mjs` sectionIds, fix the false Sam-founder-bio that the test currently locks in.

**Why I cannot mark done:**
- The `output_present` gate runs in `node_done` BEFORE the `--output` payload is persisted (`service_transition_manager.py:1038-1054` vs `:1076`).
- The gate checks `task.executions[].work_output` — empty on first call.
- `pm task done` has no `--skip-gates` flag (only `pm task queue`/`pm task claim` do).
- The pollypm test infra uses `svc.node_done(..., skip_gates=True)` via Python API, which the CLI does not expose.

**Verify:**
- `git -C ~/dev/russell/.pollypm/worktrees/russell-58 log --oneline -1`
- `cat ~/dev/russell/.pollypm/worktrees/russell-58/docs/plan/critique_maintainability.json | head -40`
- `pm task get russell/58` (context note explains the blocker)

**Suggested fix paths (pick one):**
1. Architect advances the node with admin override.
2. Add `--skip-gates` to `pm task done` in pollypm (mirrors queue/claim).
3. Re-order `node_done` to persist work_output before gate evaluation, so `output_present` becomes self-satisfying.

Standing by — won't retry blindly.
